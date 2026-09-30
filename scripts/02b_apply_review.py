"""D-4·D-5 검토 결과 적용 → private/jobs_reviewed.csv (1부 이후 단계의 기준 데이터).

검토 기록: private/review/claude_review.json (작성자 위임에 따라 Claude가 검토)
"""
import json

import pandas as pd

from common import PRIVATE_DIR, REVIEW_DIR, safe_path, verify_tasklens_unchanged, write_json

ID = "구인인증번호/ID"


def main() -> None:
    verify_tasklens_unchanged()
    rev = json.loads((REVIEW_DIR / "claude_review.json").read_text(encoding="utf-8"))
    df = pd.read_csv(PRIVATE_DIR / "jobs_clean.csv", dtype=str).fillna("")
    llm = pd.read_csv(PRIVATE_DIR / "jobs_llm.csv", dtype=str).fillna("")
    m = df.merge(llm, on=ID, how="left").fillna("")

    # D-5: 규칙이 바뀌었으므로(AI개발 우선) LLM 언급 분류에서 다시 계산한 뒤 검토 결과를 덮어쓴다
    def ai_from_mentions(s: str) -> str:
        classes = {p.rsplit("=", 1)[-1] for p in s.split("; ") if "=" in p}
        return "AI개발" if "AI개발" in classes else "도구활용" if "도구활용" in classes else "없음"
    m["AI신호"] = m["AI언급분류"].map(ai_from_mentions)
    m["최종등급"] = m["분석가능성등급"]
    m["검토메모"] = ""
    for rid, o in rev["grade_overrides"].items():
        assert (m.loc[m[ID] == rid, "분석가능성등급"] == o["from"]).all(), rid
        m.loc[m[ID] == rid, ["최종등급", "검토메모"]] = [o["to"], "등급: " + o["note"]]
    for rid, o in rev["ai_overrides"].items():
        m.loc[m[ID] == rid, "AI신호"] = o["to"]
        m.loc[m[ID] == rid, "검토메모"] += (" / " if m.loc[m[ID] == rid, "검토메모"].iloc[0] else "") + "AI: " + o["note"]
    m["검토자"] = rev["reviewer"]
    m.to_csv(safe_path(PRIVATE_DIR / "jobs_reviewed.csv"), index=False, encoding="utf-8-sig")

    # 마스킹 검토 결과 기록
    mp = REVIEW_DIR / "masking_review.csv"
    mr = pd.read_csv(mp, dtype=str).fillna("")
    mr["확인결과"] = mr.apply(lambda r: "적정" if r["구분"] == "치환" else
                              ("사업자등록번호 형식 숫자열, 개인정보 아님(행사안내 컬럼, 비공개)" if r["문맥(마스킹본)"].startswith("숫자열")
                               else "오탐"), axis=1) + " (Claude 검토)"
    try:
        mr.to_csv(safe_path(mp), index=False, encoding="utf-8-sig")
    except PermissionError:
        print("masking_review.csv가 열려 있어 확인결과를 기록하지 못함")

    summary = {"최종등급": m["최종등급"].value_counts().to_dict(), "AI신호": m["AI신호"].value_counts().to_dict(),
               "등급변경": len(rev["grade_overrides"]), "AI변경": len(rev["ai_overrides"]), "검토자": rev["reviewer"]}
    write_json(PRIVATE_DIR / "logs" / "review_summary.json", summary)
    print(summary)
    verify_tasklens_unchanged()


if __name__ == "__main__":
    main()
