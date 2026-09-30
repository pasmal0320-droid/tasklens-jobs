"""공고 과업 분석 페이지 생성: site/index.html (GitHub tasklens-jobs 저장소의 메인 페이지, 단일 파일, 데이터 내장).

입력(private/): task_matches.json, logs/model_usage.csv
공개 HTML에는 공고 본문 전체를 넣지 않는다. 과업마다 근거 원문 구절(마스킹본, 최대 80자)만 싣는다.
"""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import pandas as pd

from common import MODEL_USAGE_LOG, PRIVATE_DIR, SITE_DIR, safe_path, verify_tasklens_unchanged

TEMPLATE = Path(__file__).with_name("template_tasklens_jobs.html")


def clip(s: str, n: int) -> str:
    s = " ".join((s or "").split())
    return s if len(s) <= n else s[:n].rstrip() + "…"


def main() -> None:
    verify_tasklens_unchanged()
    m = json.loads((PRIVATE_DIR / "task_matches.json").read_text(encoding="utf-8"))
    for x in m["tasks"]:
        x["source"] = clip(x["source"], 80)
        x["cands"] = x["cands"][:1]  # 화면에는 1순위만 쓴다(2·3순위 후보 표시는 삭제)
        x.pop("alt_conflict", None)
        for c in x["cands"]:
            for o in c["occs"]:
                o.pop("ko_text", None)  # 표시용 한국어는 c['ko']에 이미 있음
    for p in m["posts"]:
        p["title"] = clip(p["title"], 70)
    usage = pd.read_csv(MODEL_USAGE_LOG)
    part2 = usage[usage["purpose"].str.startswith("T-")]
    by = part2.groupby(part2["purpose"].str.split(" ").str[0])["cost_usd"].sum()
    cost = " · ".join(f"{k} ${v:.3f}" for k, v in by.items()) + f" (합계 ${part2['cost_usd'].sum():.2f})"
    data = {"built": dt.datetime.now().strftime("%Y-%m-%d %H:%M"), "summary": m["summary"], "posts": m["posts"],
            "tasks": m["tasks"], "cost": cost}
    html = TEMPLATE.read_text(encoding="utf-8").replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    out = safe_path(SITE_DIR / "index.html")
    out.write_text(html, encoding="utf-8")
    print(f"저장: {out} ({out.stat().st_size / 1024:.0f} KB)")
    verify_tasklens_unchanged()


if __name__ == "__main__":
    main()
