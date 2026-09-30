"""D-1~D-3, D-5(규칙 단계), D-7, D-8(원문 파일·목록) — 외부 API 호출 없음.

입력: private/고용24_사무전문직_200.xlsx (채용정보 200건, 수집정보)
출력(모두 private/):
  jobs_clean.csv            정제본 (마스킹된 텍스트, 실제직무군, 대응판정, AI 신호 후보)
  postings_index.csv        공고 ID·원문 파일 경로·본문처리상태
  postings/<ID>.txt         공고별 직무내용 원문 (비공개)
  logs/d1_schema.json       스키마 검사 결과
  logs/d7_quality.json      수집 품질 요약
  review/masking_review.csv 마스킹 치환 내역과 잔여 후보 (사람 검토용)
  review/ai_signal_hits.csv AI 표현 적중 목록 (LLM 문맥 분류 전)
"""
from __future__ import annotations

import re

import pandas as pd

from common import (INFO_SHEET, PRIVATE_DIR, RAW_SHEET, RAW_XLSX, REVIEW_DIR, LOG_DIR,
                    safe_path, verify_tasklens_unchanged, write_json)
from mappings import GROUP_ORDER, RECRUIT_TO_GROUP, match_status, query_short

ID = "구인인증번호/ID"
BODY = "직무내용/상세내용"
REQUIRED = ["no", "조회직종", ID, "회사명", "공고명", "등록일", "모집직종/분야", BODY, "참고사항"]
# 마스킹 대상 자유 텍스트 컬럼 (화면 표시·외부 API 전송 가능성이 있는 것)
TEXT_COLS = [BODY, "공고명", "행사안내", "그밖의희망사항", "기타전형", "접수방법", "제출서류",
             "기타우대사항", "첨부파일/양식", "기타복리후생"]
DROP_COLS = ["인증기관연락처"]  # 기관 대표번호. 분석에 쓰지 않으므로 정제본에서 제외

# ------------------------------------------------------------------ D-3 마스킹 규칙
TITLES = "상담사|상담원|코디네이터|컨설턴트|주임|대리|과장|팀장|차장|부장|실장|국장|계장|주무관|매니저|선생님|원장|사원|위원|간사"
NOT_NAME_END = ("팀", "부", "과", "실", "센터", "본부", "지사", "사무소", "업무", "담당")
MASK_RULES: list[tuple[str, re.Pattern, str]] = [
    ("email", re.compile(r"[\w.+-]+@[\w-]+(?:\.[\w-]+)+"), "[연락처]"),
    ("url", re.compile(r"(?:https?://|www\.)[^\s가-힣<>()\[\]]+", re.I), "[링크]"),
    ("url", re.compile(r"(?<![\w@.])[a-z0-9-]+(?:\.[a-z0-9-]+)*\.(?:com|co\.kr|or\.kr|go\.kr|ac\.kr|kr|net|org|io)"
                       r"(?:/[^\s가-힣<>()]*)?(?![\w])", re.I), "[링크]"),
    ("messenger", re.compile(r"(?:카카오톡|카톡|오픈\s?채팅|kakao(?:talk)?)\s*(?:ID|아이디|채널)?\s*[:：]\s*[^\s,)]+", re.I),
     "[연락처]"),
    ("phone", re.compile(r"(?<![\d\w])0\d{1,2}[\s\-.)]{0,2}\d{3,4}[\s\-.]{0,2}\d{4}(?!\d)"), "[연락처]"),
    ("phone", re.compile(r"(?<!\d)1[5-9]\d{2}[\s\-.]\d{4}(?!\d)"), "[연락처]"),
]
# 담당자 이름: 연락 문맥 안의 "이름 + 직함" 또는 "담당자: 이름"
NAME_CTX = re.compile(r"(담당자?|문의|연락처?|접수처?|제출\s*확인)")
NAME_TITLE = re.compile(rf"(?<![가-힣])([가-힣]{{2,4}})\s?(?={TITLES})")
NAME_AFTER_LABEL = re.compile(r"(담당자\s*(?:명)?\s*[:：]\s*)([가-힣]{2,4})(?![가-힣])")


def mask_text(text: str) -> tuple[str, list[dict]]:
    """반환: (마스킹본, 치환 내역). 치환 내역에는 원문 값을 남기지 않는다."""
    log: list[dict] = []
    out = text
    for kind, pat, token in MASK_RULES:
        def _sub(m, kind=kind, token=token):
            log.append({"type": kind, "context": ""})
            return token
        out = pat.sub(_sub, out)

    def _label(m):
        name = m.group(2)
        if name.endswith(NOT_NAME_END):
            return m.group(0)
        log.append({"type": "name", "context": ""})
        return m.group(1) + "[담당자]"
    out = NAME_AFTER_LABEL.sub(_label, out)

    # 연락 문맥(앞뒤 40자) 안의 "이름 + 직함"
    spans = []
    for m in NAME_TITLE.finditer(out):
        window = out[max(0, m.start() - 40): m.end() + 40]
        name = m.group(1)
        if NAME_CTX.search(window) and "[연락처]" in window and not name.endswith(NOT_NAME_END):
            spans.append((m.start(1), m.end(1)))
    for s, e in reversed(spans):
        out = out[:s] + "[담당자]" + out[e:]
        log.append({"type": "name", "context": ""})

    for item in log:  # 검토용 문맥은 마스킹본에서 가져온다
        item["context"] = ""
    return out, log


def residual_candidates(masked: str) -> list[str]:
    """마스킹 후에도 남은 의심 패턴 (사람 검토용)."""
    found = []
    for m in re.finditer(r"\d[\d\s\-.)]{7,}\d", masked):
        digits = re.sub(r"\D", "", m.group(0))
        if len(digits) >= 9 and not re.match(r"20\d{2}", digits):
            found.append(("숫자열", m.start(), m.end()))
    for m in re.finditer(r"@", masked):
        found.append(("@", m.start(), m.end()))
    for m in NAME_TITLE.finditer(masked):
        window = masked[max(0, m.start() - 30): m.end() + 30]
        if NAME_CTX.search(window) and not m.group(1).endswith(NOT_NAME_END):
            found.append(("이름+직함", m.start(), m.end()))
    return [f"{k}: …{masked[max(0, s - 25):e + 25]}…".replace("\n", " ") for k, s, e in found]


# ------------------------------------------------------------------ D-5 AI 표현 규칙
# 영문은 대소문자 무시, 경계는 '앞뒤가 영문자가 아님'. (\b는 한글을 단어 문자로 보아 'AI를'을 놓친다)
AI_TERMS_EN = ["AI", "GPT", "ChatGPT", "OpenAI", "LLM", "Copilot", "Gemini", "Midjourney", "Stable Diffusion",
               "Perplexity", "machine learning", "deep learning", "generative"]
AI_TERMS_KO = ["인공지능", "생성형", "챗GPT", "챗지피티", "지피티", "거대언어모델", "대규모 언어모델", "코파일럿",
               "제미나이", "미드저니", "뤼튼", "머신러닝", "딥러닝"]
AI_PATTERN = re.compile(
    "|".join([rf"(?<![A-Za-z]){re.escape(t)}(?![A-Za-z])" for t in AI_TERMS_EN] + [re.escape(t) for t in AI_TERMS_KO]),
    re.I)
AI_SCAN_COLS = ["공고명", BODY, "기타우대사항"]


def main() -> None:
    verify_tasklens_unchanged()
    raw = pd.read_excel(RAW_XLSX, sheet_name=RAW_SHEET, dtype=str).fillna("")
    info = pd.read_excel(RAW_XLSX, sheet_name=INFO_SHEET, header=None, dtype=str).fillna("")

    # ---------------- D-1 스키마 검사
    missing_req = [c for c in REQUIRED if c not in raw.columns]
    if missing_req:
        raise SystemExit(f"필수 컬럼 없음: {missing_req}")
    empty = lambda s: s.str.strip().isin(["", "-"])  # noqa: E731
    schema = {
        "rows": len(raw), "cols": raw.shape[1], "expected": [200, 66],
        "id_duplicates": int(raw[ID].duplicated().sum()),
        "body_empty": int(empty(raw[BODY]).sum()),
        "missing_by_col": {c: int(empty(raw[c]).sum()) for c in raw.columns},
    }
    write_json(LOG_DIR / "d1_schema.json", schema)
    print(f"D-1 {schema['rows']}행 × {schema['cols']}열, ID 중복 {schema['id_duplicates']}, 본문 빈칸 {schema['body_empty']}")

    df = raw.drop(columns=[c for c in DROP_COLS if c in raw.columns]).copy()

    # ---------------- D-2 재분류
    df["조회직종_짧은"] = df["조회직종"].map(query_short)
    df["실제직무군"] = df["모집직종/분야"].map(RECRUIT_TO_GROUP).fillna("기타")
    df["관련직종매칭"] = df["참고사항"].str.contains("관련직종 매칭")
    df["대응판정_자동"] = [match_status(q, g) for q, g in zip(df["조회직종_짧은"], df["실제직무군"])]
    unmapped = sorted(df.loc[df["실제직무군"] == "기타", "모집직종/분야"].unique())
    print(f"D-2 실제직무군 {df['실제직무군'].nunique()}개, 미분류 모집직종 {len(unmapped)}개 {unmapped}")
    print("    관련직종 매칭", int(df["관련직종매칭"].sum()), "건 → 모두 모집직종 기준으로 분류됨:",
          sorted(df.loc[df["관련직종매칭"], "실제직무군"].value_counts().to_dict().items()))

    # ---------------- D-3 마스킹
    mask_rows = []
    counts: dict[str, int] = {}
    for col in TEXT_COLS:
        new_vals = []
        for rid, val in zip(df[ID], df[col]):
            masked, log = mask_text(val)
            new_vals.append(masked)
            for item in log:
                counts[f"{col}:{item['type']}"] = counts.get(f"{col}:{item['type']}", 0) + 1
            if log:
                for tok in ("[연락처]", "[링크]", "[담당자]"):
                    for m in re.finditer(re.escape(tok), masked):
                        mask_rows.append({"ID": rid, "컬럼": col, "구분": "치환",
                                          "문맥(마스킹본)": masked[max(0, m.start() - 30): m.end() + 30].replace("\n", " ")})
            for r in residual_candidates(masked):
                mask_rows.append({"ID": rid, "컬럼": col, "구분": "잔여 후보", "문맥(마스킹본)": r})
        df[col] = new_vals
    mdf = pd.DataFrame(mask_rows, columns=["ID", "컬럼", "구분", "문맥(마스킹본)"])
    mdf["확인결과"] = ""
    mdf.to_csv(safe_path(REVIEW_DIR / "masking_review.csv"), index=False, encoding="utf-8-sig")
    body_counts = {k.split(":")[1]: v for k, v in counts.items() if k.startswith(BODY + ":")}
    print(f"D-3 본문 치환 {body_counts} (전체 컬럼 {sum(counts.values())}건), 잔여 후보 {int((mdf['구분'] == '잔여 후보').sum())}건")

    df["본문길이"] = df[BODY].str.len()

    # ---------------- D-5 규칙 단계
    hit_rows = []
    hit_terms = []
    for _, r in df.iterrows():
        terms = []
        for col in AI_SCAN_COLS:
            for m in AI_PATTERN.finditer(r[col]):
                terms.append(m.group(0))
                hit_rows.append({"ID": r[ID], "조회직종": r["조회직종_짧은"], "컬럼": col, "표현": m.group(0),
                                 "문맥(마스킹본)": r[col][max(0, m.start() - 60): m.end() + 60].replace("\n", " ")})
        hit_terms.append(", ".join(dict.fromkeys(terms)))
    df["AI표현"] = hit_terms
    hdf = pd.DataFrame(hit_rows, columns=["ID", "조회직종", "컬럼", "표현", "문맥(마스킹본)"])
    hdf.to_csv(safe_path(REVIEW_DIR / "ai_signal_hits.csv"), index=False, encoding="utf-8-sig")
    print(f"D-5 AI 표현 적중 공고 {int((df['AI표현'] != '').sum())}건 (적중 {len(hdf)}회)")

    # ---------------- D-8 원문 파일·목록
    idx_rows = []
    for _, r in raw.iterrows():
        rid = r[ID]
        path = safe_path(PRIVATE_DIR / "postings" / f"{rid}.txt")
        path.write_text(r[BODY], encoding="utf-8")
        idx_rows.append({"ID": rid, "no": r["no"], "기관": r["회사명"], "등록일": r["등록일"],
                         "조회직종": query_short(r["조회직종"]), "원문파일": str(path.relative_to(PRIVATE_DIR)),
                         "마스킹md": "", "본문처리상태": "대기", "사유": ""})
    idx_path = PRIVATE_DIR / "postings_index.csv"
    if idx_path.exists():  # 이미 처리된 상태는 보존 (재개 가능)
        old = pd.read_csv(idx_path, dtype=str).fillna("").set_index("ID")
        for row in idx_rows:
            if row["ID"] in old.index and old.at[row["ID"], "본문처리상태"] != "대기":
                for k in ("마스킹md", "본문처리상태", "사유"):
                    row[k] = old.at[row["ID"], k]
    pd.DataFrame(idx_rows).to_csv(safe_path(idx_path), index=False, encoding="utf-8-sig")
    print(f"D-8 원문 파일 {len(idx_rows)}개, 목록 저장")

    # ---------------- D-7 수집 품질 요약
    info_map = {a.strip(): b.strip() for a, b in info.values[1:] if a.strip() and a.strip() != "nan"}
    comp = df["회사명"].value_counts()
    quality = {
        "행수": len(df), "ID중복": schema["id_duplicates"],
        "고유회사": int(comp.size), "최다공고회사_건수": int(comp.max()), "2건회사수": int((comp == 2).sum()),
        "등록일분포": df["등록일"].value_counts().sort_index().to_dict(),
        "조회직종별": df["조회직종_짧은"].value_counts().to_dict(),
        "실제직무군별": df["실제직무군"].value_counts().reindex(GROUP_ORDER).dropna().astype(int).to_dict(),
        "대응판정_자동": df["대응판정_자동"].value_counts().to_dict(),
        "본문길이_중앙값_조회직종별": df.groupby("조회직종_짧은")["본문길이"].median().round().astype(int).to_dict(),
        "결측비율": {c: round(schema["missing_by_col"][c] / len(df), 3) for c in
                   ["기업형태/규모", "경력", "학력", "고용형태", "관련직종", "행사안내"]},
        "수집정보": {k: info_map.get(k, "미기록") for k in
                 ["출처", "수집 시작 시각", "수집 종료 시각", "요청 건수", "조건 - 정보제공처", "조건 - 등록일",
                  "조건 - 고용형태", "조건 - 정렬", "선별 방식", "선별 규칙", "합계", "제외 컬럼"]},
        "원문대조": "미확인" if not (REVIEW_DIR / "source_check.csv").exists() else "있음",
    }
    write_json(LOG_DIR / "d7_quality.json", quality)
    print(f"D-7 회사 {quality['고유회사']}곳(최다 {quality['최다공고회사_건수']}건), 등록일 {len(quality['등록일분포'])}개")

    df.to_csv(safe_path(PRIVATE_DIR / "jobs_clean.csv"), index=False, encoding="utf-8-sig")
    print("저장: private/jobs_clean.csv")
    verify_tasklens_unchanged()


if __name__ == "__main__":
    main()
