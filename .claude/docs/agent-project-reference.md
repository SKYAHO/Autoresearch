# Agent Project Reference

> Last Updated: 2026-07-06

프로젝트 구조, 폴더 책임, 팀 소유권을 빠르게 찾기 위한 문서입니다.
"X는 어디에 있는가?", "Y는 누가 소유하는가?" 질문에 답합니다.

## When To Use This Doc

- 프로젝트 레이아웃과 폴더별 책임을 파악해야 할 때
- 새 코드를 추가할 위치를 정해야 할 때
- 팀 도메인 경계와 소유권을 확인해야 할 때

## Project Layout

현재 저장소의 실제 구조입니다:

```
autoresearch/                # 런타임 패키지
├── youtube_collection/      # YouTube 트렌딩 수집 파이프라인
│   ├── fetch.py             # YouTube Data API 호출
│   ├── transform.py         # 원본 → 정제 데이터 변환
│   ├── load.py              # GCS 데이터 레이크 적재
│   ├── backfill.py          # 과거 데이터 백필
│   ├── schema.py            # pydantic 데이터 계약
│   └── client.py            # 복원력 레이어(재시도/Key롤링/IP밴시그니처/프록시)
├── proxy/                   # Cloud Run dumb forwarder (IP밴 egress seam)
├── virtual_users/           # Gemini 기반 가상 유저(페르소나) 생성
│   ├── persona_source.py    # 페르소나 원천 데이터 로드
│   ├── interests.py         # 관심사 매핑
│   ├── gemini_generator.py  # Gemini API 호출
│   ├── pipeline.py          # 생성 파이프라인 오케스트레이션
│   └── schema.py            # pydantic 데이터 계약
└── math_utils.py            # 공용 계산 유틸리티

dags/                        # Airflow DAG (Astro Runtime 13.8.0)
├── youtube_trending_kr_daily.py
└── youtube_backfill_kr.py

tests/                       # 모듈별 test_<module>.py 플랫 구조
examples/ctr_pipeline_scaffold/  # CTR 파이프라인 예제 스캐폴드
feature_repo/                # Feast Entity·FeatureView 정의 (더미 스키마), feature_store.yaml
scripts/                     # 더미 데이터 적재·Feast 조회 검증 스크립트
docs/                        # 스펙·플랜 문서
.github/                     # CI, Claude 리뷰, 이슈 폼, PR 템플릿
```

진행 중(별도 브랜치, main 미반영):

```
src/                         # CTR 학습 파이프라인 (Issue #33)
├── models/                  # LightGBM 모델 클래스
├── features/                # 피처 엔지니어링, Feast 정의
├── pipeline/                # train/evaluate/config.yaml
└── utils/                   # 모델 저장/로드 유틸리티
```

## Team Ownership & Domains

| 도메인 | 팀원 | 책임 | 주요 경로 |
|---|---|---|---|
| **Model Training** | waieiches, hyochangsung | 모델 구조, 학습 파이프라인, 평가 지표 | `src/models/`, `src/pipeline/` (진행 중), `examples/ctr_pipeline_scaffold/` |
| **Feast Features** | waieiches, hyochangsung | 피처 정의(ODFV), 피처 엔지니어링, 피처 스토어 연동 | `feature_repo/`, `src/features/` (진행 중) |
| **Airflow Orchestration** | bbungjun | DAG 정의, 스케줄링, 데이터 파이프라인 오케스트레이션 | `dags/`, `autoresearch/youtube_collection/` |
| **GCP Infrastructure** | hyeongyu-data | 클라우드 배포, 인프라, 시크릿 관리 | `.github/workflows/`, GCS/BigQuery 설정 |

## Ownership Boundaries

### `autoresearch/youtube_collection/`
- **책임:** YouTube API 수집, 변환, GCS 적재, 백필
- **패턴:** fetch → transform → load 단계를 파일로 분리합니다. 데이터
  계약은 `schema.py`의 pydantic 모델로 정의합니다.

### `autoresearch/virtual_users/`
- **책임:** 페르소나 원천 데이터 로드, Gemini 기반 가상 유저 생성
- **패턴:** 외부 API 호출(`gemini_generator.py`)과 오케스트레이션
  (`pipeline.py`)을 분리합니다.

### `dags/`
- **책임:** Airflow DAG 정의만 담습니다. 비즈니스 로직은
  `autoresearch/` 모듈에 두고 DAG은 호출만 합니다.
- **주의:** DAG은 sys.path 조작으로 `autoresearch`를 import 합니다.
  컨테이너 배치(`Dockerfile`) 변경 시 함께 확인해야 합니다.

### `tests/`
- **책임:** 모듈별 단위 테스트. `tests/test_<module>.py` 형식을
  따릅니다. 새 모듈에는 대응하는 테스트 파일을 만듭니다.

## Technical Stack

- **언어:** Python 3.12 (`.python-version`), CI는 3.11/3.12 매트릭스
- **의존성:** uv + `pyproject.toml`/`uv.lock`(단일 출처).
  `requirements.txt`·`proxy/requirements.txt`는 `uv export` 산출물
- **주요 라이브러리:** pydantic v2, pyarrow, google-api-python-client,
  google-cloud-storage, gcsfs, google-genai(개발)
- **데이터 저장:** GCS 데이터 레이크(parquet), BigQuery(프로덕션 예정)
- **오케스트레이션:** Airflow (Astro Runtime 13.8.0)
- **테스트:** pytest
- **피처 스토어:** Feast 0.64 (`feature_repo/`, BigQuery offline / Redis
  online, 더미 스키마)
- **계획:** LightGBM (별도 브랜치 진행 중)

## Key Extension Rules

1. **도메인 소유권 확인:** Model Training, Feast, Airflow, GCP 중
   어디에 속하는지 먼저 판단합니다.
2. **올바른 위치에 배치:** 위 폴더 구조를 따르고 도메인 간 결합을
   피합니다.
3. **데이터 계약 갱신:** 스키마가 바뀌면 해당 모듈의 `schema.py`
   pydantic 모델과 테스트를 함께 수정합니다.
4. **테스트 작성:** `tests/test_<module>.py`에 단위 테스트를 추가합니다.
5. **설계 결정 기록:** 아키텍처에 영향이 있으면 `docs/specs/`에 spec을
   남기거나 관련 `.claude/docs/` 가이드를 갱신합니다.

## Verification Checklist

- [ ] 코드가 팀 도메인에 맞는 폴더에 있다.
- [ ] DAG에 비즈니스 로직이 들어가지 않았다.
- [ ] 스키마 변경 시 pydantic 모델과 테스트를 함께 수정했다.
- [ ] 새 기능에 테스트가 있다.
- [ ] 동작·설정이 바뀌었으면 문서를 갱신했다.
