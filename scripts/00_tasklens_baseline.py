"""작업 시작 전 TaskLens 폴더의 파일 해시 기준값을 저장한다 (읽기만 함).

이미 기준값이 있으면 덮어쓰지 않고 비교만 한다.
"""
from common import HASH_BASELINE, tasklens_hashes, verify_tasklens_unchanged, write_json

if HASH_BASELINE.exists():
    verify_tasklens_unchanged()
else:
    hashes = tasklens_hashes()
    write_json(HASH_BASELINE, hashes)
    print(f"기준값 저장: {HASH_BASELINE} ({len(hashes)}개 파일)")
