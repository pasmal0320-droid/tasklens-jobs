"""T-4 공고 과업 임베딩(한국어 원문 / 영어 번역) → T-5 상위 3개 매칭 → T-6 검증 표본 추출.

- 검토 기록(private/review/t2_review.json)의 삭제·추가를 반영한 과업 목록을 쓴다.
- 질의(공고 과업) 쪽에만 검색 지시문을 붙인다. 기준 문장(TaskLens)은 지시문 없이 임베딩했다(05).
- 상위 3개는 '고유 문장' 단위라 같은 문장이 순위를 중복 차지하지 않는다.
출력(private/): tasks_final.csv, task_query_emb.npz, match_candidates.json, review/match_validation.csv(판정 칸 비어 있음)
"""
from __future__ import annotations

import json
import random

import numpy as np
import pandas as pd

from common import PRIVATE_DIR, RANDOM_STATE, REVIEW_DIR, embed, safe_path, verify_tasklens_unchanged, write_json

INSTRUCT = ("Instruct: Given a job duty from a job posting, retrieve the O*NET task statement that describes "
            "the same work\nQuery: ")
EMB_DIR = PRIVATE_DIR / "tl_emb"
N_VALID = 40  # 조정용 20 + 확인용 20


def load_master() -> tuple[np.ndarray, list[dict]]:
    sents = json.loads((PRIVATE_DIR / "tl_sentences.json").read_text(encoding="utf-8"))
    chunks = sorted(EMB_DIR.glob("chunk_*.npy"))
    X = np.concatenate([np.load(c).astype(np.float32) for c in chunks])
    assert len(X) == len(sents), f"임베딩 {len(X)} ≠ 문장 {len(sents)} (05를 끝까지 실행)"
    return X, sents


def final_tasks() -> pd.DataFrame:
    t = pd.read_csv(PRIVATE_DIR / "extracted_tasks.csv", dtype=str).fillna("")
    rev = json.loads((REVIEW_DIR / "t2_review.json").read_text(encoding="utf-8"))
    for r in rev["remove"]:
        hit = (t["ID"] == r["ID"]) & (t["task_ko"] == r["task_ko"])
        assert hit.sum() == 1, r
        t = t[~hit]
    add = pd.DataFrame([{"ID": a["ID"], "k": "99", "task_ko": a["task_ko"], "task_en": a["task_en"], "source": a["source"],
                         "start": "", "end": "", "원문위치": "확인", "검토": "추가(Claude)"} for a in rev["add"]])
    t = pd.concat([t, add], ignore_index=True).fillna("")
    t["tid"] = [f"{i}-{n}" for n, i in enumerate(t["ID"])]
    return t.reset_index(drop=True)


def main() -> None:
    verify_tasklens_unchanged()
    X, sents = load_master()
    t = final_tasks()
    t.to_csv(safe_path(PRIVATE_DIR / "tasks_final.csv"), index=False, encoding="utf-8-sig")
    print(f"T-4 과업 {len(t)}개 (공고 {t['ID'].nunique()}건)")

    cache_p = PRIVATE_DIR / "task_query_emb.npz"
    if cache_p.exists() and len(np.load(cache_p)["ko"]) == len(t):
        Q = dict(np.load(cache_p))
    else:
        Q = {}
        for lang, col in (("ko", "task_ko"), ("en", "task_en")):
            vecs = []
            texts = [INSTRUCT + s for s in t[col]]
            for b in range(0, len(texts), 64):
                vecs += embed(texts[b:b + 64], purpose=f"T-4 공고 과업 임베딩({lang})", ref=f"{lang}{b // 64}")
            v = np.array(vecs, dtype=np.float32)
            Q[lang] = v / np.linalg.norm(v, axis=1, keepdims=True)
        np.savez(safe_path(cache_p), **Q)

    cand = {}
    for lang in ("ko", "en"):
        S = Q[lang] @ X.T
        top = np.argsort(-S, axis=1)[:, :3]
        for i, tid in enumerate(t["tid"]):
            cand.setdefault(tid, {})[lang] = [{"sent": int(j), "sim": round(float(S[i, j]), 4), "text": sents[j]["text"],
                                               "task_ids": sents[j]["task_ids"]} for j in top[i]]
    write_json(PRIVATE_DIR / "match_candidates.json", cand)
    for lang in ("ko", "en"):
        s1 = np.array([cand[x][lang][0]["sim"] for x in t["tid"]])
        print(f"T-5 {lang}: 1순위 유사도 중앙값 {np.median(s1):.3f}, 사분위 {np.percentile(s1, 25):.3f}~{np.percentile(s1, 75):.3f}")

    # T-6 검증 표본: 직무군 × 유사도 구간(번역 방식 1순위 기준 삼분위)을 고르게, 시드 고정
    jobs = pd.read_csv(PRIVATE_DIR / "jobs_reviewed.csv", dtype=str).fillna("").set_index("구인인증번호/ID")
    t["group"] = t["ID"].map(jobs["실제직무군"])
    t["sim_en"] = [cand[x]["en"][0]["sim"] for x in t["tid"]]
    t["band"] = pd.qcut(t["sim_en"], 3, labels=["낮음", "중간", "높음"])
    rng = random.Random(RANDOM_STATE)
    picked = []
    strata = {k: list(g["tid"]) for k, g in t.groupby(["group", "band"], observed=True)}
    keys = sorted(strata)
    for v in strata.values():
        rng.shuffle(v)
    while len(picked) < N_VALID:
        for k in keys:
            if strata[k] and len(picked) < N_VALID:
                picked.append(strata[k].pop())
    rng.shuffle(picked)
    rows = []
    by = t.set_index("tid")
    for n, tid in enumerate(picked):
        r = by.loc[tid]
        row = {"순번": n + 1, "용도": "조정용" if n < N_VALID // 2 else "확인용", "tid": tid, "ID": r["ID"],
               "직무군": r["group"], "유사도구간": r["band"], "과업(한국어)": r["task_ko"], "과업(번역)": r["task_en"]}
        for lang in ("ko", "en"):
            for k, c in enumerate(cand[tid][lang]):
                row[f"{lang}{k + 1}"] = f"{c['sim']:.3f} | {c['text']}"
            row[f"{lang}_1순위판정"] = ""
            row[f"{lang}_타당후보"] = ""
        row["판정이유"] = ""
        rows.append(row)
    pd.DataFrame(rows).to_csv(safe_path(REVIEW_DIR / "match_validation.csv"), index=False, encoding="utf-8-sig")
    print(f"T-6 검증 표본 {len(rows)}개 저장 (조정용 {N_VALID // 2}, 확인용 {N_VALID // 2})")
    verify_tasklens_unchanged()


if __name__ == "__main__":
    main()
