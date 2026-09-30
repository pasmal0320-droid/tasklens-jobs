"""T-3 기준 과업 마스터·임베딩 (TaskLens data.json 읽기 전용).

출력(private/):
  tasklens_task_master.csv   과업 한 줄씩 (task_id 고유)
  tasklens_version.json      TaskLens meta.built_at·sources
  tl_sentences.json          정규화한 고유 문장 → 연결 task_id 목록
  tl_emb/chunk_XXXX.npy      고유 문장 임베딩(float16, L2 정규화). 청크 단위라 중단 후 이어서 실행 가능
"""
from __future__ import annotations

import json
import re

import numpy as np
import pandas as pd

from common import PRIVATE_DIR, TASKLENS_DATA, embed, safe_path, verify_tasklens_unchanged, write_json

CHUNK = 64
EMB_DIR = PRIVATE_DIR / "tl_emb"


def norm(t: str) -> str:
    return re.sub(r"\s+", " ", t or "").strip().lower()


def main() -> None:
    verify_tasklens_unchanged()
    with open(TASKLENS_DATA, encoding="utf-8") as f:  # 읽기 전용
        data = json.load(f)
    rows = []
    for o in data["occupations"]:
        for t in o["tasks"]:
            rows.append({"task_id": t["id"], "onet_soc_code": o["code"], "occupation_en": o["title_en"],
                         "occupation_ko": o.get("title_ko", ""), "occ_review": o.get("review", ""),
                         "occ_beta": o.get("exp"), "task_text_en": t["text_en"], "task_text_ko": t.get("text_ko", ""),
                         "task_type": t.get("type", ""), "gpt4_label": t.get("exp_label", ""),
                         "obs_share": t.get("obs_share"), "auto": t.get("auto"), "aei_match": t["aei_match"]})
    m = pd.DataFrame(rows)
    assert m["task_id"].is_unique
    m.to_csv(safe_path(PRIVATE_DIR / "tasklens_task_master.csv"), index=False, encoding="utf-8-sig")
    write_json(PRIVATE_DIR / "tasklens_version.json", {"built_at": data["meta"]["built_at"], "sources": data["meta"]["sources"],
                                                      "match": data["meta"]["match"], "n_occupations": len(data["occupations"]),
                                                      "n_tasks": len(m)})
    m["norm"] = m["task_text_en"].map(norm)
    groups = m.groupby("norm", sort=True)
    sents = [{"i": i, "text": g["task_text_en"].iloc[0], "task_ids": g["task_id"].tolist()}
             for i, (_, g) in enumerate(groups)]
    write_json(PRIVATE_DIR / "tl_sentences.json", sents)
    multi = sum(len(s["task_ids"]) > 1 for s in sents)
    print(f"과업 {len(m)}개, 고유 문장 {len(sents)}개(여러 직업에 붙은 문장 {multi}개)")

    EMB_DIR.mkdir(exist_ok=True)
    n_chunks = (len(sents) + CHUNK - 1) // CHUNK
    done = 0
    for c in range(n_chunks):
        p = EMB_DIR / f"chunk_{c:04d}.npy"
        if p.exists():
            done += 1
            continue
        texts = [s["text"] for s in sents[c * CHUNK:(c + 1) * CHUNK]]
        v = np.array(embed(texts, purpose="T-3 TaskLens 과업 임베딩", ref=f"chunk{c}"), dtype=np.float32)
        v /= np.linalg.norm(v, axis=1, keepdims=True)
        np.save(safe_path(p), v.astype(np.float16))
        done += 1
        if done % 20 == 0:
            print(f"  {done}/{n_chunks} 청크")
    print(f"임베딩 완료 {n_chunks} 청크")
    verify_tasklens_unchanged()


if __name__ == "__main__":
    main()
