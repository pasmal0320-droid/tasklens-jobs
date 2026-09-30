"""JobMap 공통 설정: 경로, 쓰기 경로 검사, TaskLens 읽기 전용 확인.

PRD v1.4 §4.1 폴더 규칙
- C:\\claude\\tasklens 는 읽기 전용이다.
- 새로 만드는 모든 파일은 C:\\claude\\JobMap 아래에 둔다.
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
from pathlib import Path

JOBMAP_ROOT = Path(os.environ.get("JOBMAP_ROOT", r"C:\claude\JobMap")).resolve()
SITE_DIR = JOBMAP_ROOT / "site"
PRIVATE_DIR = Path(os.environ.get("JOBMAP_PRIVATE_DIR", JOBMAP_ROOT / "private")).resolve()
TASKLENS_DIR = Path(r"C:\claude\tasklens").resolve()
TASKLENS_DATA = Path(os.environ.get("TASKLENS_DATA", TASKLENS_DIR / "data.json")).resolve()

RAW_XLSX = PRIVATE_DIR / "고용24_사무전문직_200.xlsx"
RAW_SHEET = "채용정보 200건"
INFO_SHEET = "수집정보"
LOG_DIR = PRIVATE_DIR / "logs"
REVIEW_DIR = PRIVATE_DIR / "review"
HASH_BASELINE = LOG_DIR / "tasklens_hashes_before.txt"
ENV_FILE = JOBMAP_ROOT / "OpenRouter.env"

EMBED_MODEL = "qwen/qwen3-embedding-8b"
LLM_MODEL = "qwen/qwen3.7-plus"
RANDOM_STATE = 42


def _inside(path: Path, root: Path) -> bool:
    try:
        path.resolve().relative_to(root)
        return True
    except ValueError:
        return False


if not _inside(PRIVATE_DIR, JOBMAP_ROOT):
    raise SystemExit(f"JOBMAP_PRIVATE_DIR가 JobMap 밖을 가리킵니다: {PRIVATE_DIR}")


def safe_path(path: str | Path) -> Path:
    """쓰기 대상 경로를 검사한다. JobMap 밖이거나 TaskLens 안이면 즉시 중단."""
    p = Path(path).resolve()
    if _inside(p, TASKLENS_DIR):
        raise SystemExit(f"[중단] TaskLens 폴더에는 쓸 수 없습니다: {p}")
    if not _inside(p, JOBMAP_ROOT):
        raise SystemExit(f"[중단] JobMap 밖에는 쓸 수 없습니다: {p}")
    p.parent.mkdir(parents=True, exist_ok=True)
    return p


def write_text(path: str | Path, text: str) -> Path:
    p = safe_path(path)
    p.write_text(text, encoding="utf-8")
    return p


def write_json(path: str | Path, obj) -> Path:
    return write_text(path, json.dumps(obj, ensure_ascii=False, indent=2))


def tasklens_hashes() -> dict[str, str]:
    """TaskLens 폴더 전체 파일의 SHA-256 (읽기만 함)."""
    out = {}
    for f in sorted(TASKLENS_DIR.rglob("*")):
        if f.is_file():
            h = hashlib.sha256()
            with f.open("rb") as fh:
                for chunk in iter(lambda: fh.read(1 << 20), b""):
                    h.update(chunk)
            out[str(f.relative_to(TASKLENS_DIR))] = h.hexdigest()
    return out


def verify_tasklens_unchanged() -> None:
    """작업 시작 시 저장한 기준값과 비교. 다르면 중단."""
    if not HASH_BASELINE.exists():
        raise SystemExit("TaskLens 해시 기준값이 없습니다. 00_tasklens_baseline.py를 먼저 실행하세요.")
    before = json.loads(HASH_BASELINE.read_text(encoding="utf-8"))
    now = tasklens_hashes()
    if before != now:
        changed = sorted(set(before) ^ set(now) | {k for k in before.keys() & now.keys() if before[k] != now[k]})
        raise SystemExit(f"[중단] TaskLens 폴더가 바뀌었습니다: {changed[:20]}")
    print(f"TaskLens 변경 없음 확인 ({len(now)}개 파일)")


# ---------------------------------------------------------------- OpenRouter
OPENROUTER_URL = "https://openrouter.ai/api/v1"
API_INPUT_LOG = LOG_DIR / "api_inputs.jsonl"
MODEL_USAGE_LOG = LOG_DIR / "model_usage.csv"


def api_key() -> str:
    """환경변수 우선, 없으면 JobMap/OpenRouter.env. 값은 어디에도 출력·기록하지 않는다."""
    key = os.environ.get("OPENROUTER_API_KEY", "").strip()
    if not key and ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8-sig").splitlines():
            k, _, v = line.strip().partition("=")
            if k.strip() == "OPENROUTER_API_KEY":
                key = v.strip().strip("\"'")
    if not key:
        raise SystemExit(f"OPENROUTER_API_KEY가 없습니다. 환경변수 또는 {ENV_FILE}에 넣어 주세요.")
    return key


_LOG_LOCK = threading.Lock()


def _log_call(purpose: str, model: str, inputs, usage: dict, ref: str = "") -> None:
    with _LOG_LOCK:
        _log_call_unlocked(purpose, model, inputs, usage, ref)


def _log_call_unlocked(purpose: str, model: str, inputs, usage: dict, ref: str = "") -> None:
    import csv
    import datetime as dt

    now = dt.datetime.now().isoformat(timespec="seconds")
    p = safe_path(API_INPUT_LOG)
    with p.open("a", encoding="utf-8") as f:
        f.write(json.dumps({"at": now, "purpose": purpose, "model": model, "ref": ref, "inputs": inputs},
                           ensure_ascii=False) + "\n")
    p = safe_path(MODEL_USAGE_LOG)
    new = not p.exists()
    with p.open("a", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["at", "purpose", "model", "ref", "prompt_tokens", "completion_tokens", "cost_usd"])
        w.writerow([now, purpose, model, ref, usage.get("prompt_tokens", 0),
                    usage.get("completion_tokens", 0), usage.get("cost", "")])


def _post(path: str, payload: dict, retries: int = 4) -> dict:
    import time

    import requests

    for attempt in range(retries):
        try:
            r = requests.post(f"{OPENROUTER_URL}{path}", timeout=180, json=payload,
                              headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"})
            if r.status_code == 200:
                j = r.json()
                if "error" not in j:
                    return j
                err = str(j["error"])[:300]
            else:
                err = f"HTTP {r.status_code}: {r.text[:300]}"
        except Exception as e:  # 네트워크 오류
            err = repr(e)[:300]
        wait = 5 * (attempt + 1)
        print(f"  재시도 {attempt + 1}/{retries} ({wait}s): {err}")
        time.sleep(wait)
    raise RuntimeError(f"OpenRouter 호출 실패: {path}")


def embed(texts: list[str], purpose: str, ref: str = "", model: str = EMBED_MODEL) -> list[list[float]]:
    """임베딩. 입력은 반드시 마스킹본이어야 한다(호출자가 보장, 로그로 사후 확인)."""
    j = _post("/embeddings", {"model": model, "input": texts})
    _log_call(purpose, model, texts, j.get("usage", {}), ref)
    return [d["embedding"] for d in sorted(j["data"], key=lambda d: d["index"])]


def chat_json(system: str, user: str, purpose: str, ref: str = "", model: str = LLM_MODEL,
              temperature: float = 0.0) -> dict:
    """JSON 응답을 요구하는 LLM 호출. 파싱 실패 시 한 번 더 요청."""
    payload = {"model": model, "temperature": temperature,
               "response_format": {"type": "json_object"},
               "messages": [{"role": "system", "content": system}, {"role": "user", "content": user}]}
    last = ""
    for _ in range(2):
        j = _post("/chat/completions", payload)
        _log_call(purpose, model, [system, user], j.get("usage", {}), ref)
        last = j["choices"][0]["message"]["content"] or ""
        txt = last.strip()
        if txt.startswith("```"):
            txt = txt.strip("`").removeprefix("json").strip()
        try:
            return json.loads(txt)
        except json.JSONDecodeError:
            continue
    raise RuntimeError(f"JSON 파싱 실패 ({ref}): {last[:200]}")
