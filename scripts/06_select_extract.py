"""T-1 분석 대상 선정 + T-2 과업 추출(한국어) + 영어 번역(T-4 비교용).

T-1: 최종등급 A·B 공고를 실제직무군(층)별로 시드 고정 무작위 추출, 층마다 PER_GROUP건.
T-2: LLM(qwen/qwen3.7-plus)이 마스킹 본문에서 수행 과업을 뽑고, 근거 원문 구절을 그대로 인용한다.
     스크립트가 원문에서 인용 위치를 찾아 저장한다(공백 차이 무시). 찾지 못하면 '원문 위치 미확인'.
출력(private/): t1_selection.json, extracted_tasks.csv, extract/<ID>.json
"""
from __future__ import annotations

import json
import re
from concurrent.futures import ThreadPoolExecutor

import pandas as pd

from common import LLM_MODEL, PRIVATE_DIR, RANDOM_STATE, chat_json, safe_path, verify_tasklens_unchanged, write_json

ID = "구인인증번호/ID"
BODY = "직무내용/상세내용"
PER_GROUP = 4
OUT_DIR = PRIVATE_DIR / "extract"

SYSTEM = """당신은 채용공고의 업무 문장을 과업 단위로 나누는 연구 보조원입니다. JSON만 출력합니다.
{"tasks": [{"task_ko": "<수행 과업 한 개. '~하기/~관리' 같은 짧은 한국어 동사구>",
            "source": "<그 과업의 근거가 되는 원문 구절을 본문에서 글자 그대로 복사(10~80자)>",
            "task_en": "<task_ko를 O*NET 과업 문장 문체의 영어로 번역. 동사 원형으로 시작>"}]}
규칙:
- 이 직무가 실제로 수행하는 업무만 뽑습니다. 자격요건·우대사항·근로조건·급여·주소·복리후생·회사 소개·전형 절차는 제외합니다.
- 한 구절에 서로 다른 업무가 여럿이면 나눕니다(예: "전표 입력 및 부가세 신고" → 2개). 같은 업무를 반복하지 않습니다.
- 본문에 없는 업무를 추측해 넣지 않습니다. 직무명만 있고 업무가 없으면 빈 목록.
- task_en은 번역만 하고 내용을 덧붙이지 않습니다."""


def locate(body: str, quote: str) -> tuple[int, int]:
    words = (quote or "").split()
    if not words:
        return -1, -1
    m = re.compile(r"\s*".join(re.escape(w) for w in words)).search(body)
    return (m.start(), m.end()) if m else (-1, -1)


def extract(rid: str, title: str, body: str) -> dict:
    p = OUT_DIR / f"{rid}.json"
    if p.exists():
        return json.loads(p.read_text(encoding="utf-8"))
    out = chat_json(SYSTEM, f"공고명: {title}\n\n[본문]\n{body}", purpose="T-2 과업 추출·번역", ref=rid)
    tasks = []
    for t in out.get("tasks", []):
        if not isinstance(t, dict) or not str(t.get("task_ko", "")).strip():
            continue
        s, e = locate(body, t.get("source", ""))
        tasks.append({"task_ko": t["task_ko"].strip(), "task_en": str(t.get("task_en", "")).strip(),
                      "source": t.get("source", ""), "start": s, "end": e})
    res = {"ID": rid, "model": LLM_MODEL, "tasks": tasks}
    write_json(p, res)
    return res


def main() -> None:
    verify_tasklens_unchanged()
    df = pd.read_csv(PRIVATE_DIR / "jobs_reviewed.csv", dtype=str).fillna("")
    ab = df[df["최종등급"].isin(["A", "B"])]
    sel = pd.concat([g.sample(n=min(PER_GROUP, len(g)), random_state=RANDOM_STATE)
                     for _, g in ab.groupby("실제직무군", sort=True)])
    selection = {"seed": RANDOM_STATE, "per_group": PER_GROUP, "population": len(df), "ab_candidates": len(ab),
                 "selected": len(sel), "by_group": sel["실제직무군"].value_counts().to_dict(),
                 "ab_by_group": ab["실제직무군"].value_counts().to_dict(),
                 "not_selected_ab": len(ab) - len(sel), "c_excluded": int((df["최종등급"] == "C").sum()),
                 "ids": sel[ID].tolist()}
    write_json(PRIVATE_DIR / "t1_selection.json", selection)
    print(f"T-1 모집단 {len(df)}, A·B 후보 {len(ab)}, 선정 {len(sel)} (직무군당 {PER_GROUP}, seed {RANDOM_STATE})")

    OUT_DIR.mkdir(exist_ok=True)
    with ThreadPoolExecutor(max_workers=6) as ex:
        results = list(ex.map(lambda r: extract(r[ID], r["공고명"], r[BODY]), [r for _, r in sel.iterrows()]))
    rows = []
    for res in results:
        for k, t in enumerate(res["tasks"]):
            rows.append({"ID": res["ID"], "k": k, **t, "원문위치": "확인" if t["start"] >= 0 else "원문 위치 미확인"})
    tdf = pd.DataFrame(rows)
    tdf.to_csv(safe_path(PRIVATE_DIR / "extracted_tasks.csv"), index=False, encoding="utf-8-sig")
    zero = [r["ID"] for r in results if not r["tasks"]]
    print(f"T-2 과업 {len(tdf)}개, 공고당 평균 {len(tdf) / len(results):.1f}, 과업 0개 공고 {len(zero)}, "
          f"원문 위치 미확인 {(tdf['원문위치'] != '확인').sum()}")
    verify_tasklens_unchanged()


if __name__ == "__main__":
    main()
