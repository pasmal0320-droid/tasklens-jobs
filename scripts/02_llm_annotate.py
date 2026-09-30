"""D-4 분석 가능성 등급, D-5 AI 표현 문맥 분류, D-8 제목 구조화·2줄 요약 (LLM: qwen/qwen3.7-plus).

- 입력은 01_prepare.py가 만든 마스킹본만 사용한다.
- 공고별 결과를 private/postings/<ID>.llm.json 에 저장하고 postings_index.csv 의 본문처리상태를 갱신한다.
  이미 '완료'인 공고는 건너뛰므로 중단 후 다시 실행하면 이어서 처리한다.
- 사용법: python 02_llm_annotate.py [--batch 30] [--workers 4]
  --batch N : 이번 실행에서 최대 N건만 처리 (기본: 남은 전부를 30건 배치로 연속 처리)
"""
from __future__ import annotations

import argparse
import json
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from common import LLM_MODEL, PRIVATE_DIR, REVIEW_DIR, chat_json, safe_path, verify_tasklens_unchanged, write_json

ID = "구인인증번호/ID"
BODY = "직무내용/상세내용"
HEADINGS = ["회사소개", "담당업무", "자격요건", "우대사항", "근무조건", "복리후생", "전형·접수", "기타"]
AI_CLASSES = ["도구활용", "AI개발", "회사소개", "기타언급"]

SYSTEM = f"""당신은 한국 채용공고를 분석하는 연구 보조원입니다. 주어진 공고 본문(연락처는 이미 가려져 있음)을 읽고 JSON만 출력합니다.
원문을 고치거나 새로 지어내지 마십시오. 본문에 없는 내용은 추측하지 않습니다.

출력 형식:
{{
  "sections": [{{"heading": "<{'|'.join(HEADINGS)} 중 하나>", "starts_with": "<그 구획이 시작되는 원문 문자열을 정확히 그대로 12~25자>"}}],
  "tasks": ["<본문에 적힌, 이 직무가 실제로 수행하는 업무 하나를 짧게>", ...],
  "grade_reason": "<등급 근거 한 문장>",
  "summary": ["<요약 1줄>", "<요약 2줄>"],
  "ai_mentions": [{{"expr": "<표현>", "class": "<{'|'.join(AI_CLASSES)}>", "reason": "<근거 한 문장>"}}]
}}

규칙:
- sections: 본문 순서대로. starts_with는 본문에 글자 그대로 존재해야 합니다(공백·기호 포함). 구획이 하나뿐이면 하나만.
- tasks: '수행 과업'만 넣습니다. 직무명만 있는 경우(예: "경리 모집"), 자격요건·우대사항·근무조건·급여·복리후생·회사 소개·전형 절차는 제외합니다.
  서로 다른 업무는 따로 적고, 같은 업무를 반복하지 않습니다. 수행 과업을 확인할 수 없으면 빈 목록.
- summary: 원문에 근거한 2줄 요약(각 60자 이내). 연락처·사람 이름은 넣지 않습니다.
- ai_mentions: 사용자 메시지의 'AI 표현 후보'가 있을 때만 각 후보를 분류합니다. 없으면 빈 목록.
  도구활용 = 이 직무를 수행할 때 AI 도구(ChatGPT 등)를 쓰도록 요구·우대·명시함
  AI개발 = 이 직무의 업무가 AI 모델·AI 서비스·AI 학습 데이터를 개발·구축·운영함
  회사소개 = 회사·제품·사업을 소개하는 문장 속 언급일 뿐, 이 직무의 업무·요건이 아님
  기타언급 = 위에 해당하지 않는 언급(예: AI 관련 교육·영업·일반 소개)"""


def build_user(r: pd.Series, hits: pd.DataFrame) -> str:
    lines = [f"공고명: {r['공고명']}", f"모집직종: {r['모집직종/분야']}", "", "[본문]", r[BODY]]
    if len(hits):
        lines += ["", "[AI 표현 후보]"]
        for _, h in hits.iterrows():
            lines.append(f"- 표현 '{h['표현']}' ({h['컬럼']}): …{h['문맥(마스킹본)']}…")
    return "\n".join(lines)


def structure_md(rid: str, title: str, body: str, sections: list[dict]) -> tuple[str, list[str], str]:
    """원문(마스킹본)에 제목만 삽입. 반환: (md, 찾지 못한 starts_with, 담당업무 본문)."""
    def find(sw: str, start: int) -> int:
        """공백 차이(줄바꿈·연속 공백)를 무시하고 원문 위치를 찾는다."""
        words = sw.split()
        if not words:
            return -1
        m = re.compile(r"\s*".join(re.escape(w) for w in words)).search(body, start)
        return m.start() if m else -1

    cuts, missing, pos = [], [], 0
    for s in sections:
        sw = (s.get("starts_with") or "").strip()
        h = s.get("heading") if s.get("heading") in HEADINGS else "기타"
        i = find(sw, pos)
        if i < 0:
            i = find(sw, 0)
        if i < 0:
            missing.append(sw)
            continue
        cuts.append((i, h))
        pos = i + 1
    cuts = sorted(set(cuts))
    if not cuts or cuts[0][0] > 0:
        cuts.insert(0, (0, cuts[0][1] if cuts and not body[:cuts[0][0]].strip() else "기타"))
    parts, duty = [], []
    for k, (start, h) in enumerate(cuts):
        end = cuts[k + 1][0] if k + 1 < len(cuts) else len(body)
        seg = body[start:end]
        parts.append((h, seg))
        if h == "담당업무":
            duty.append(seg.strip())
    # 검증: 제목을 빼면 원문과 같아야 함
    assert "".join(seg for _, seg in parts) == body, rid
    md = f"# {title}\n\n- 공고 ID: {rid}\n- 이 파일은 연락처를 가린 본문에 구획 제목만 붙인 것이며 원문 문장은 바꾸지 않았다.\n\n"
    md += "".join(f"\n## {h}\n\n{seg.strip()}\n" for h, seg in parts if seg.strip())
    return md, missing, "\n".join(duty)


def process(r: pd.Series, hits: pd.DataFrame) -> dict:
    rid = r[ID]
    out = chat_json(SYSTEM, build_user(r, hits), purpose="D-4/D-5/D-8 공고 주석", ref=rid)
    tasks = [t.strip() for t in out.get("tasks", []) if isinstance(t, str) and t.strip()]
    grade = "A" if len(tasks) >= 2 else "B" if len(tasks) == 1 else "C"
    md, missing, duty = structure_md(rid, r["공고명"], r[BODY], out.get("sections", []))
    mentions = [m for m in out.get("ai_mentions", []) if isinstance(m, dict)]
    for m in mentions:
        if m.get("class") not in AI_CLASSES:
            m["class"] = "기타언급"
    classes = {m["class"] for m in mentions}
    ai = "AI개발" if "AI개발" in classes else "도구활용" if "도구활용" in classes else "없음"
    res = {"ID": rid, "model": LLM_MODEL, "grade": grade, "tasks": tasks, "grade_reason": out.get("grade_reason", ""),
           "summary": [s for s in out.get("summary", []) if isinstance(s, str)][:2], "ai_mentions": mentions,
           "ai_signal_llm": ai, "sections": out.get("sections", []), "sections_not_found": missing,
           "duty_text": duty}
    write_json(PRIVATE_DIR / "postings" / f"{rid}.llm.json", res)
    safe_path(PRIVATE_DIR / "postings" / f"{rid}.md").write_text(md, encoding="utf-8")
    return res


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--batch", type=int, default=0)
    ap.add_argument("--workers", type=int, default=4)
    ap.add_argument("--restructure", action="store_true", help="LLM 호출 없이 저장된 구획으로 md·업무본문만 다시 만든다")
    args = ap.parse_args()
    verify_tasklens_unchanged()
    if args.restructure:
        restructure()
        return

    df = pd.read_csv(PRIVATE_DIR / "jobs_clean.csv", dtype=str).fillna("")
    hits = pd.read_csv(REVIEW_DIR / "ai_signal_hits.csv", dtype=str).fillna("")
    idx_path = PRIVATE_DIR / "postings_index.csv"
    idx = pd.read_csv(idx_path, dtype=str).fillna("")
    pending = idx.loc[idx["본문처리상태"].isin(["대기", "실패"]), "ID"].tolist()
    if args.batch:
        pending = pending[:args.batch]
    print(f"처리 대상 {len(pending)}건 (모델 {LLM_MODEL})")
    rows = df.set_index(ID)

    for b in range(0, len(pending), 30):  # 30건 배치 단위로 저장
        batch = pending[b:b + 30]
        with ThreadPoolExecutor(max_workers=args.workers) as ex:
            futs = {ex.submit(process, pd.concat([pd.Series({ID: rid}), rows.loc[rid]]),
                              hits[hits["ID"] == rid]): rid for rid in batch}
            for f in as_completed(futs):
                rid = futs[f]
                i = idx.index[idx["ID"] == rid][0]
                try:
                    res = f.result()
                    idx.at[i, "본문처리상태"] = "완료"
                    idx.at[i, "마스킹md"] = f"postings/{rid}.md"
                    idx.at[i, "사유"] = ("구획 시작 문자열 일부 미발견" if res["sections_not_found"] else "")
                except Exception as e:  # 실패는 사유를 남기고 다음 실행에서 재시도
                    idx.at[i, "본문처리상태"] = "실패"
                    idx.at[i, "사유"] = repr(e)[:200]
        idx.to_csv(safe_path(idx_path), index=False, encoding="utf-8-sig")
        done = (idx["본문처리상태"] == "완료").sum()
        print(f"  배치 {b // 30 + 1}: {len(batch)}건 처리 → 누적 완료 {done}/{len(idx)}, 실패 {(idx['본문처리상태'] == '실패').sum()}")

    build_outputs(df, idx)
    verify_tasklens_unchanged()


def restructure() -> None:
    df = pd.read_csv(PRIVATE_DIR / "jobs_clean.csv", dtype=str).fillna("").set_index(ID)
    idx_path = PRIVATE_DIR / "postings_index.csv"
    idx = pd.read_csv(idx_path, dtype=str).fillna("")
    fixed = 0
    for i, rid in idx["ID"].items():
        p = PRIVATE_DIR / "postings" / f"{rid}.llm.json"
        res = json.loads(p.read_text(encoding="utf-8"))
        md, missing, duty = structure_md(rid, df.at[rid, "공고명"], df.at[rid, BODY], res["sections"])
        fixed += bool(res["sections_not_found"]) and not missing
        res.update({"sections_not_found": missing, "duty_text": duty})
        write_json(p, res)
        safe_path(PRIVATE_DIR / "postings" / f"{rid}.md").write_text(md, encoding="utf-8")
        idx.at[i, "사유"] = "구획 시작 문자열 일부 미발견" if missing else ""
    idx.to_csv(safe_path(idx_path), index=False, encoding="utf-8-sig")
    print(f"재구성 완료: 미발견 해소 {fixed}건, 남은 미발견 {(idx['사유'] != '').sum()}건")
    build_outputs(df.reset_index(), idx)


def _save(frame: pd.DataFrame, path) -> None:
    """검토 파일이 Excel 등에서 열려 있으면 _new 이름으로 저장한다."""
    try:
        frame.to_csv(safe_path(path), index=False, encoding="utf-8-sig")
    except PermissionError:
        alt = path.with_name(path.stem + "_new.csv")
        frame.to_csv(safe_path(alt), index=False, encoding="utf-8-sig")
        print(f"  {path.name}가 열려 있어 {alt.name}로 저장")


def build_outputs(df: pd.DataFrame, idx: pd.DataFrame) -> None:
    """공고별 결과를 모아 정제본 주석 파일과 사람 검토 파일을 만든다."""
    recs = []
    for rid in idx.loc[idx["본문처리상태"] == "완료", "ID"]:
        recs.append(json.loads((PRIVATE_DIR / "postings" / f"{rid}.llm.json").read_text(encoding="utf-8")))
    if not recs:
        return
    ann = pd.DataFrame([{
        ID: r["ID"], "분석가능성등급": r["grade"], "과업수_LLM": len(r["tasks"]), "과업_LLM": " | ".join(r["tasks"]),
        "등급근거": r["grade_reason"], "요약": " / ".join(r["summary"]), "AI신호_LLM": r["ai_signal_llm"],
        "AI언급분류": "; ".join(f"{m.get('expr')}={m.get('class')}" for m in r["ai_mentions"]),
        "업무본문": r["duty_text"], "업무본문_출처": "담당업무 구획" if r["duty_text"] else "구획 없음(전체 본문 사용 예정)",
    } for r in recs])
    ann.to_csv(safe_path(PRIVATE_DIR / "jobs_llm.csv"), index=False, encoding="utf-8-sig")

    m = df.merge(ann, on=ID, how="inner")
    # 사람 검토: B·C 등급
    g = m[m["분석가능성등급"].isin(["B", "C"])][
        [ID, "조회직종_짧은", "실제직무군", "공고명", "분석가능성등급", "과업_LLM", "등급근거", BODY]].copy()
    g[BODY] = g[BODY].str.slice(0, 400)
    g["확인등급"] = ""
    g["제외사유"] = ""
    _save(g, REVIEW_DIR / "grade_review.csv")
    # 사람 검토: AI 표현 적중 공고 전부
    a = m[m["AI표현"] != ""][[ID, "조회직종_짧은", "공고명", "AI표현", "AI언급분류", "AI신호_LLM"]].copy()
    hits = pd.read_csv(REVIEW_DIR / "ai_signal_hits.csv", dtype=str).fillna("")
    ctx = hits.groupby("ID")["문맥(마스킹본)"].apply(lambda s: " ‖ ".join(s.head(3)))
    a["문맥(마스킹본)"] = a[ID].map(ctx)
    a["확인AI신호"] = ""
    _save(a, REVIEW_DIR / "ai_signal_review.csv")
    print(f"등급: {m['분석가능성등급'].value_counts().to_dict()} / AI신호_LLM: {m['AI신호_LLM'].value_counts().to_dict()}")
    print(f"담당업무 구획 없음: {(ann['업무본문'] == '').sum()}건 / 검토 파일: review/grade_review.csv({len(g)}), "
          f"review/ai_signal_review.csv({len(a)})")


if __name__ == "__main__":
    main()
