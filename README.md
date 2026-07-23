# Autoresearch

YouTube 트렌딩 데이터 기반 CTR(Click-Through Rate) 모델링 프로젝트입니다.
YouTube 트렌딩 영상을 수집하고, LLM으로 가상 유저와 action log를 생성해
CTR 모델을 학습·서빙하며, 모델 노출 결과가 다시 학습 데이터로 돌아오는
일일 폐루프를 운영합니다.

## 비전

Autoresearch의 최종 목표는 ML 리서처·엔지니어를 위한 **자율 실험 에이전트
서비스**입니다. 사용자가 가설 한 줄(예: 추천 알고리즘 논문)을 입력하면,
에이전트가 raw 데이터로 피처를 재조립·가공하고, 모델·임베딩 방식을 선택해
학습한 뒤, origin(champion) 모델과의 비교·A/B 테스트까지 스스로 판단해
수행합니다. 현재 운영 중인 일일 폐루프는 이 에이전트가 실험을 돌리기 위한
기반 테스트베드이며, MVP(폐루프 완주) → 최적화 → 기술 고도화 → 에이전트
자율 실험 순서로 나아갑니다.

전체 파이프라인 (일일 폐루프):

```
YouTube 수집 → 가상 유저 생성 → action log 생성 → CTR 학습 데이터셋 → 모델 학습/평가
                    ↑                                                        ↓
            노출·클릭 시뮬레이션 ← 일일 추천 ← 리랭킹 서빙 API (GKE) ← 모델 배포
```

## 저장소 구조

```
autoresearch/        # 런타임 패키지
├── youtube_collection/   # YouTube 트렌딩 수집 (fetch/transform/load/backfill + 복원력 레이어)
├── virtual_users/        # LLM 기반 가상 유저(페르소나) 생성
├── action_logs/          # action log 생성·shard·merge·품질 계약
└── jobs/                 # Airflow 비종속 공개 batch CLI
src/                 # CTR 학습·서빙 파이프라인
├── features/             # 피처 엔지니어링·조립
├── models/               # LightGBM 모델
├── pipeline/             # 학습·평가·학습 데이터셋·일일 추천·정책 시뮬레이션
├── serving/              # FastAPI 리랭킹 추론 서버
├── tracking/             # MLflow tracking·registry 연동
└── utils/                # 모델 저장/로드 유틸리티
proxy/               # Cloud Run dumb forwarder (YouTube API IP밴 대응)
deploy/              # 배포 산출물 (mlflow/ Tracking Server, serving/ 추론 이미지)
feature_repo/        # Feast 피처 스토어 정의 (BigQuery offline / Redis online)
examples/            # CTR 파이프라인 예제 스캐폴드
scripts/             # 검증·일회성 스크립트
tests/               # 모듈별 단위 테스트 (플랫 구조)
docs/                # 문서 — docs/README.md 인덱스 참조
```

## 배포 이미지

| 이미지 | 용도 |
|---|---|
| `Dockerfile.app` | 공개 batch CLI 실행 (Airflow가 소비하는 canonical application image) |
| `Dockerfile.train` | CTR 모델 학습 (GCS code archive 부트스트랩, MLflow 연동) |
| `Dockerfile.feast` | Feast apply/materialize (feast 격리 그룹 전용) |
| `deploy/serving/Dockerfile` | 리랭킹 서빙 API (GKE) |
| `deploy/mlflow/Dockerfile` | MLflow Tracking Server |

DAG·스케줄·Airflow 배포는 [`SKYAHO/Autoresearch-airflow`](https://github.com/SKYAHO/Autoresearch-airflow),
GCP 인프라는 [`SKYAHO/Autoresearch-infra`](https://github.com/SKYAHO/Autoresearch-infra)가 소유합니다.

action log 데이터 레이크는 **일일 슬라이스 파티션**(`dt=D` = KST D일
하루치, 파티션 간 서로소)으로 적재되며, 피처·학습 소비자는 `dt BETWEEN`
프루닝으로 30일 히스토리를 조립합니다. 계약 상세:
[`docs/specs/2026-07-24-action-log-slice-semantics.md`](docs/specs/2026-07-24-action-log-slice-semantics.md)

## 팀 도메인

| 도메인 | 팀원 | 주요 경로 |
|---|---|---|
| Model Training | waieiches, hyochangsung | `src/models/`, `src/pipeline/`, `src/tracking/` |
| Feast Features | waieiches, hyochangsung | `feature_repo/`, `src/features/` |
| YouTube Collection & Release | Noah-JuYong | `autoresearch/youtube_collection/`, `proxy/`, `.github/workflows/` (release·배포 트리거) |
| Airflow Orchestration | bbungjun | `Autoresearch-airflow` 저장소 |
| GCP Infrastructure | hyeongyu-data | `Autoresearch-infra` 저장소 |

> `src/serving/`(리랭킹 API)과 정책 라운드·일일 추천 폐루프의 도메인 소유는
> 아직 미지정입니다 — 저장소 구조 논의(#149)에서 확정 예정.

## 시작하기

```bash
uv sync                                    # .venv 생성 + 의존성 설치 (uv.lock 기준)
uv run python -m pytest                    # 테스트 실행 (CI와 동일)
uv run --no-sync ruff check autoresearch tests tools   # lint (CI와 동일)
```

- Python 3.12 (`.python-version`), 의존성 단일 출처는 `pyproject.toml` + `uv.lock`
- 필수 환경 변수는 `.env.example` 참조
- Feast 작업은 격리 그룹 사용: `uv sync --only-group feast`

## 문서

- 문서 인덱스: [`docs/README.md`](docs/README.md)
- 기여 규칙(브랜치·이슈·PR 전략): [`CONTRIBUTING.md`](CONTRIBUTING.md)
- AI 코딩 에이전트 가이드: [`CLAUDE.md`](CLAUDE.md)
