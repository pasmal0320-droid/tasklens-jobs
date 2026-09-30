# TaskLens 공고 과업 분석

**고용24 채용공고의 업무 문장을 과업 단위로 나눠, AI가 도울 수 있는 일을 살펴보는 페이지**

🔗 https://pasmal0320-droid.github.io/tasklens-jobs/

> 적용 가능성은 대체가 아니다. 여기서 보는 'AI 적용 가능'(AI만으로 가능·도구가 있으면 가능)은 GPT-4가 평가한 작업 시간 단축 가능성이고, '대화에서 확인됨'은 특정 서비스(Claude)의 대화에 그 과업이 나타났다는 뜻이다.

강병석 · KAIST 기술경영전문대학원 ITM.690 인공지능 경영과 법 (2026 가을)

| 과제 | 페이지 | 저장소 |
|---|---|---|
| 1. TaskLens — AI 과업 노출 탐색기 | https://pasmal0320-droid.github.io/ | `pasmal0320-droid.github.io` |
| 2. TaskLens 공고 과업 분석 (이 저장소) | https://pasmal0320-droid.github.io/tasklens-jobs/ | `tasklens-jobs` |

이 페이지는 1번 TaskLens를 확장한 것이다. TaskLens가 미국 표준 직업(O\*NET)의 과업 단위로 AI 적용 가능성과 Claude 대화 확인 여부를 보여 준다면, 이 페이지는 한국 채용공고에 실제로 적힌 일을 같은 기준과 같은 용어로 읽는다. TaskLens 저장소와 데이터는 수정하지 않고 링크로만 연결한다.

## 무엇을 분석했나

1. **모은 공고:** 2026년 9월 고용24에 등록된 사무·전문직 10개 직종 공고 200건
2. **걸러 낸 공고:** 업무 내용 없이 직무명·근무조건만 적힌 23건을 빼고 177건
3. **분석한 공고:** 12개 직무 분야마다 4건씩 무작위(seed 42)로 뽑은 48건

공고의 업무 설명을 일 단위로 나눠 과업 244개를 뽑고, 과업마다 가장 비슷한 O\*NET 과업 문장을 임베딩으로 찾았다(222개 연결). 연결된 표준 과업에 TaskLens가 붙여 둔 AI 적용 가능성 평가(GPT-4)와 Claude 대화 확인 여부를 가져와 공고별·직무군별로 정리했다.

## 페이지 기능

- **첫 화면:** 분석 대상을 고른 과정, 숫자 카드 4장(분석 공고·과업·연결·평가)
- **요약:**
  - 과업 판정 분포(AI만으로 가능 / 도구가 있으면 가능 / 해당 없음 / 지표 없음 / 매칭 불가)
  - AI 적용 가능 과업이 50% 이상인 공고와 공고별 분포
  - 직무군별 AI 적용 가능 과업 비중과 대화에서 확인된 과업
- **공고 목록과 상세:**
  - 직무군·고용형태·AI 신호로 거르고, 정렬할 수 있음
  - 공고를 누르면 원문 구절 → 나눈 과업 → 연결된 표준 과업 → AI 적용 가능성·대화 확인 여부 순서로 볼 수 있음
  - AI 적용 가능성(0~1): 이 공고와 비슷한 표준 직업의 값을 나란히 보여 줌(TaskLens 직업 화면과 같은 값)
- **자주 연결된 O\*NET 과업과 직업:** 직업은 TaskLens 직업 화면으로 이동
- 모든 수치는 분자/분모로 표시하고 하나의 점수로 합치지 않는다.
- 하단에 "이 결과는 채용 심사·인사 평가 용도로 사용할 수 없습니다"를 고정 표시한다.

## 파일 구성

```
index.html   페이지 본체 (HTML·CSS·JS·데이터를 한 파일에 내장)
README.md    이 문서
scripts/     데이터 처리·페이지 생성 스크립트
.gitignore   원본·중간 데이터가 올라가지 않도록 막는 설정
```

외부 리소스: Chart.js 4.4.1(cdnjs), Pretendard 웹폰트(jsDelivr). 서버와 실시간 API 호출은 없다.

## 데이터와 공개 범위

- 원본 공고 데이터(xlsx), 공고 본문 전체, 중간 결과, 임베딩, 로그, API 키는 이 저장소에 없다. 작성자의 로컬 비공개 폴더에만 있다.
- 페이지에는 집계 결과와 과업마다 근거가 된 원문 구절(연락처·링크·담당자 이름을 가린 마스킹본, 80자 이하)만 싣고, 구인인증번호를 함께 표시한다.
- 공고 데이터는 기존 수집본 200건이며 이번 작업에서 추가 수집하지 않았다. 수집 기록상 대상 사이트의 robots.txt가 자동 접근을 제한하므로, 확장할 때는 고용24 공식 Open API 등 허용된 경로를 쓴다.

## 재현 (작성자 로컬 환경)

스크립트는 `C:\claude\JobMap\private\`(비공개 데이터)와 `C:\claude\tasklens\data.json`(TaskLens, 읽기 전용)을 읽는다. API 키는 환경변수 `OPENROUTER_API_KEY` 또는 `JobMap\OpenRouter.env`(배포 제외)에서 읽는다.

```bash
python 00_tasklens_baseline.py   # TaskLens 파일 해시 기준값 (읽기만)
python 01_prepare.py             # 스키마 검사·직무군 분류·마스킹·AI 표현 탐지·공고별 원문 파일
python 02_llm_annotate.py        # 분석 가능성 등급(A/B/C)·구획·요약 (LLM, 30건 배치)
python 02b_apply_review.py       # 등급·AI 신호 검토 결과 적용
python 05_tasklens_master.py     # TaskLens data.json → 과업 마스터, 고유 문장 17,578개 임베딩
python 06_select_extract.py      # 직무군별 4건 선정(seed 42) + 과업 추출·영어 번역
python 07_match.py               # 과업 임베딩, 상위 3개 매칭, 검증 표본 40개
python 08_link.py                # 매칭 방식·기준 적용(영어 번역, 유사도 0.69), TaskLens 지표 연결
python 09_build_tasklens_jobs.py # index.html 생성
```

보호 장치(`scripts/common.py`):
- 쓰기 경로가 JobMap 밖이면 멈춘다.
- 단계마다 TaskLens 파일 해시를 비교해 변경이 없는지 확인한다.
- 외부 API에는 마스킹한 본문만 보낸다.

## 모델과 비용

| 용도 | 모델 (OpenRouter) | 비용(USD) |
|---|---|---|
| 등급 판정·구획·요약, 과업 추출·번역, O\*NET 문장 한국어 표시 | `qwen/qwen3.7-plus` | 약 1.15 |
| 공고·과업·TaskLens 과업 임베딩 | `qwen/qwen3-embedding-8b` (4096차원) | 약 0.004 |

합계 약 $1.23이며, 재실행·재시도를 포함한 누적값이다.

## 검토와 한계

- LLM의 1차 판정(분석 가능성 등급, AI 신호, 과업 추출, 매칭 검증)은 작성자 위임에 따라 Claude가 검토했다. 판정자는 한 명이다.
- 매칭 방식(영어 번역)과 기준 유사도(0.69)는 검증 표본 40개(조정용 20·확인용 20)로 정했다. 확인용에서 기준 이상 18개 중 14개가 타당했다.
- 직무 분야마다 공고 4건이라 분야 간 비교는 참고용이다. O\*NET은 미국 직업 체계라 한국 공고의 업무와 정확히 맞지 않을 수 있다.
- O\*NET 과업 문장의 한국어는 TaskLens 검수 번역이 있으면 그것을, 없으면 AI 번역을 표시한다.

## 출처

| 데이터 | 버전 |
|---|---|
| 고용24 채용정보 | 2026년 9월 등록 사무·전문직 공고 200건 (기존 수집본) |
| TaskLens 가공 데이터 | 2026-09-27 빌드 |
| [O\*NET Database](https://www.onetcenter.org/database.html) | 31.0 (CC BY 4.0, USDOL/ETA) |
| [GPTs are GPTs](https://github.com/openai/GPTs-are-GPTs) (Eloundou 외, 2023) | GPT-4 평가 라벨 (MIT) |
| [Anthropic Economic Index](https://huggingface.co/datasets/Anthropic/EconomicIndex) | release 2025-03-27 (데이터 CC-BY) |

교육 목적의 분석이며 특정 기업·공고에 대한 평가가 아니다.
