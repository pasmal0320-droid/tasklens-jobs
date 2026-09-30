"""T-6 결정 적용 → T-7 TaskLens 지표 연결 → T-8 결과 저장, 공고·직무군 지표(K-1~K-6) 계산.

입력(private/): tasks_final.csv, match_candidates.json, tasklens_task_master.csv, tasklens_version.json,
               review/match_validation.csv(판정 완료), review/t6_decision.json(방식·기준 유사도)
출력: private/task_matches.json
"""
from __future__ import annotations

import json
from collections import Counter

import numpy as np
import pandas as pd

from common import LLM_MODEL, PRIVATE_DIR, REVIEW_DIR, chat_json, verify_tasklens_unchanged, write_json

OBSERVED = {"exact", "shared"}
UNCHECKABLE = {"unlinked", "multi"}
GAP = 0.01  # 2·3순위와 1순위 유사도 차이가 이보다 작고 등급이 다르면 '후보 간 등급 불일치'


def sentence_info(task_ids: list[int], master: pd.DataFrame) -> dict:
    rows = master.loc[[i for i in task_ids if i in master.index]]
    labels = sorted({l for l in rows["gpt4_label"] if l})
    if not labels:
        label = "지표 없음"
    elif len(labels) > 1:
        label = "문장 내 등급 불일치"
    else:
        label = labels[0]
    st = set(rows["aei_match"])
    if st & OBSERVED:
        obs = "관측"
        autos = rows.loc[rows["aei_match"].isin(OBSERVED), "auto"].dropna()
        auto = float(autos.iloc[0]) if len(autos) else None
        share = rows.loc[rows["aei_match"].isin(OBSERVED), "obs_share"].dropna()
        obs_share = float(share.iloc[0]) if len(share) else None
    elif "unobserved" in st:
        obs, auto, obs_share = "관측되지 않음", None, None
    else:
        obs, auto, obs_share = "확인 불가", None, None
    occs = [{"code": r.onet_soc_code, "en": r.occupation_en, "ko": r.occupation_ko, "beta": r.occ_beta,
             "label": r.gpt4_label, "task_id": int(i), "ko_text": r.task_text_ko, "review": r.occ_review}
            for i, r in rows.iterrows()]
    return {"label": label, "labels": labels, "obs": obs, "auto": auto, "obs_share": obs_share, "occs": occs}


def translate(texts: list[str]) -> dict[str, str]:
    """TaskLens 검수 번역이 없는 O*NET 과업 문장의 한국어 번역 (표시용, AI 번역)."""
    out = {}
    for b in range(0, len(texts), 40):
        chunk = texts[b:b + 40]
        res = chat_json("O*NET 과업 문장을 자연스러운 한국어 과업 문장으로 번역합니다. 내용을 더하거나 빼지 않습니다. "
                        'JSON만 출력: {"ko": ["<번역1>", ...]} (입력과 같은 순서·개수)',
                        "\n".join(f"{i + 1}. {t}" for i, t in enumerate(chunk)), purpose="T-8 O*NET 과업 번역", ref=f"tr{b // 40}")
        ko = res.get("ko", [])
        if len(ko) != len(chunk):
            raise RuntimeError("번역 개수 불일치")
        out.update(dict(zip(chunk, ko)))
    return out


def main() -> None:
    verify_tasklens_unchanged()
    dec = json.loads((REVIEW_DIR / "t6_decision.json").read_text(encoding="utf-8"))
    method, thr = dec["method"], float(dec["threshold"])
    t = pd.read_csv(PRIVATE_DIR / "tasks_final.csv", dtype=str).fillna("")
    cand = json.loads((PRIVATE_DIR / "match_candidates.json").read_text(encoding="utf-8"))
    master = pd.read_csv(PRIVATE_DIR / "tasklens_task_master.csv").set_index("task_id")
    master["gpt4_label"] = master["gpt4_label"].fillna("")
    master["task_text_ko"] = master["task_text_ko"].fillna("")
    master["occupation_ko"] = master["occupation_ko"].fillna("")
    val = pd.read_csv(REVIEW_DIR / "match_validation.csv", dtype=str).fillna("").set_index("tid")
    jobs = pd.read_csv(PRIVATE_DIR / "jobs_reviewed.csv", dtype=str).fillna("").set_index("구인인증번호/ID")
    sel = json.loads((PRIVATE_DIR / "t1_selection.json").read_text(encoding="utf-8"))
    version = json.loads((PRIVATE_DIR / "tasklens_version.json").read_text(encoding="utf-8"))

    tasks = []
    for _, r in t.iterrows():
        cs = cand[r["tid"]][method]
        infos = [sentence_info(c["task_ids"], master) for c in cs]
        top = cs[0]
        status, why = "매칭", ""
        judged = val.at[r["tid"], f"{method}_1순위판정"] if r["tid"] in val.index else ""
        if top["sim"] < thr:
            status, why = "매칭 불가", f"유사도 {top['sim']:.3f} < 기준 {thr:.3f}"
        elif judged == "부적절":
            status, why = "매칭 불가", "검증 표본에서 1순위 후보가 의미상 부적절로 판정"
        label = infos[0]["label"] if status == "매칭" else "매칭 불가"
        alt_conflict = status == "매칭" and label in ("E0", "E1", "E2") and any(
            inf["label"] in ("E0", "E1", "E2") and inf["label"] != label and top["sim"] - c["sim"] < GAP
            for c, inf in zip(cs[1:], infos[1:]))
        tasks.append({"tid": r["tid"], "ID": r["ID"], "task_ko": r["task_ko"], "task_en": r["task_en"], "source": r["source"],
                      "added": r.get("검토", ""), "status": status, "why": why, "label": label,
                      "obs": infos[0]["obs"] if status == "매칭" else None,
                      "auto": infos[0]["auto"] if status == "매칭" else None,
                      "obs_share": infos[0]["obs_share"] if status == "매칭" else None,
                      "alt_conflict": bool(alt_conflict), "validated": judged or None,
                      "cands": [{"sim": c["sim"], "text": c["text"], "label": inf["label"], "obs": inf["obs"],
                                 "auto": inf["auto"], "occs": inf["occs"][:6], "n_occs": len(inf["occs"])} for c, inf in zip(cs, infos)]})

    # 한국어 표시 문장: 검수 번역 우선, 없으면 AI 번역
    need = sorted({c["text"] for x in tasks for c in x["cands"] if not any(o["ko_text"] for o in c["occs"])})
    tr_p = PRIVATE_DIR / "onet_ko_ai.json"
    tr = json.loads(tr_p.read_text(encoding="utf-8")) if tr_p.exists() else {}
    missing = [s for s in need if s not in tr]
    if missing:
        tr.update(translate(missing))
        write_json(tr_p, tr)
    for x in tasks:
        for c in x["cands"]:
            rev = next((o["ko_text"] for o in c["occs"] if o["ko_text"]), "")
            c["ko"], c["ko_source"] = (rev, "검수 번역") if rev else (tr.get(c["text"], ""), "AI 번역")

    # ---------------- 공고 단위 지표
    posts = []
    for rid in sel["ids"]:
        xs = [x for x in tasks if x["ID"] == rid]
        lab = Counter(x["label"] for x in xs)
        graded = lab["E0"] + lab["E1"] + lab["E2"]
        matched = [x for x in xs if x["status"] == "매칭"]
        checkable = [x for x in matched if x["obs"] in ("관측", "관측되지 않음")]
        observed = [x for x in matched if x["obs"] == "관측"]
        occ = Counter()
        for x in matched:
            for o in x["cands"][0]["occs"]:
                occ[(o["code"], o["en"], o["ko"], o["beta"])] += 1 / max(1, x["cands"][0]["n_occs"])
        top_occ = [{"code": k[0], "en": k[1], "ko": k[2], "beta": k[3], "weight": round(v, 2)} for k, v in occ.most_common(3)]
        j = jobs.loc[rid]
        posts.append({
            "ID": rid, "title": j["공고명"], "company": j["회사명"], "group": j["실제직무군"], "query": j["조회직종_짧은"],
            "emp": j["고용형태"], "ai": j["AI신호"], "grade": j["최종등급"],
            "n_tasks": len(xs), "n_matched": len(matched), "n_unmatched": lab["매칭 불가"], "n_noindex": lab["지표 없음"],
            "n_conflict": lab["문장 내 등급 불일치"], "E0": lab["E0"], "E1": lab["E1"], "E2": lab["E2"], "graded": graded,
            "k2": round((lab["E1"] + lab["E2"]) / graded, 4) if graded else None,
            "k2_e1": round(lab["E1"] / graded, 4) if graded else None,
            "beta": round((lab["E1"] + 0.5 * lab["E2"]) / graded, 4) if graded else None,
            "k5_obs": len(observed), "k5_checkable": len(checkable),
            "k5_auto_major": sum(1 for x in observed if x["auto"] is not None and x["auto"] >= 0.5),
            "k5_aug_major": sum(1 for x in observed if x["auto"] is not None and x["auto"] < 0.5),
            "k5_unknown": sum(1 for x in observed if x["auto"] is None),
            "caution": len(xs) <= 2 or (graded / len(xs) < 0.5 if xs else True),
            "top_occ": top_occ,
        })
    k2s = [p for p in posts if p["k2"] is not None]
    k3_base = [p for p in k2s if p["graded"] >= 3]
    total = len(tasks)
    matched_n = sum(x["status"] == "매칭" for x in tasks)
    graded_n = sum(x["label"] in ("E0", "E1", "E2") for x in tasks)
    summary = {
        "k1": {"population": sel["population"], "ab": sel["ab_candidates"], "selected": sel["selected"],
               "analyzed": len(posts), "c_excluded": sel["c_excluded"], "not_selected_ab": sel["not_selected_ab"],
               "tasks": total, "matched": matched_n, "graded": graded_n,
               "zero_task_posts": sum(p["n_tasks"] == 0 for p in posts)},
        "k3": {"num": sum(p["k2"] >= 0.5 for p in k3_base), "den": len(k3_base),
               "excluded_few": len(k2s) - len(k3_base), "not_computable": len(posts) - len(k2s)},
        "labels": dict(Counter(x["label"] for x in tasks)),
        "obs": dict(Counter(x["obs"] for x in tasks if x["status"] == "매칭")),
        "alt_conflict": sum(x["alt_conflict"] for x in tasks),
        "method": method, "threshold": thr, "decision": dec, "tasklens_version": version,
        "llm_model": LLM_MODEL, "selection": sel,
    }
    write_json(PRIVATE_DIR / "task_matches.json", {"summary": summary, "posts": posts, "tasks": tasks})
    print(json.dumps({k: summary[k] for k in ("k1", "k3", "labels", "obs", "alt_conflict")}, ensure_ascii=False))
    verify_tasklens_unchanged()


if __name__ == "__main__":
    main()
