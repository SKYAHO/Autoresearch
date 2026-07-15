# Autoresearch

YouTube 트렌딩 데이터 기반 CTR(Click-Through Rate) 모델링 프로젝트입니다.
YouTube 트렌딩 영상을 수집하고, LLM으로 가상 유저와 action log를 생성해
CTR 학습 데이터셋을 만들고, LightGBM 모델을 학습·평가합니다.

전체 파이프라인:

```
YouTube 수집 → 가상 유저 생성 → action log 생성 → CTR 학습 데이터셋 → 모델 학습/평가
```

## 저장소 구조

```
autoresearch/        # 런타임 패키지
├── youtube_collection/   # YouTube 트렌딩 수집 (fetch/transform/load/backfill + 복원력 레이어)
├── virtual_users/        # LLM 기반 가상 유저(페르소나) 생성
├── action_logs/          # action log 생성·shard·merge·품질 계약
└── jobs/                 # Airflow 비종속 공개 batch CLI
src/                 # CTR 학습 파이프라인 (features/models/pipeline/tracking/utils)
proxy/               # Cloud Run dumb forwarder (YouTube API IP밴 대응)
deploy/mlflow/       # MLflow Tracking Server 이미지·로컬 개발 환경
feature_repo/        # Feast 피처 스토어 정의
examples/            # CTR 파이프라인 예제 스캐폴드
scripts/             # 검증·일회성 스크립트
tests/               # 모듈별 단위 테스트 (플랫 구조)
docs/                # 문서 — docs/README.md 인덱스 참조
```

`Dockerfile.app`이 공개 CLI를 실행하는 canonical application image입니다.
DAG·스케줄·Airflow 배포는 [`SKYAHO/Autoresearch-airflow`](https://github.com/SKYAHO/Autoresearch-airflow),
GCP 인프라는 [`SKYAHO/Autoresearch-infra`](https://github.com/SKYAHO/Autoresearch-infra)가 소유합니다.

## 팀 도메인

| 도메인 | 팀원 | 주요 경로 |
|---|---|---|
| Model Training | waieiches, hyochangsung | `src/models/`, `src/pipeline/` |
| Feast Features | waieiches, hyochangsung | `feature_repo/`, `src/features/` |
| Airflow Orchestration | bbungjun | `Autoresearch-airflow` 저장소 |
| GCP Infrastructure | hyeongyu-data | `Autoresearch-infra` 저장소 |

## 시작하기

```bash
uv sync                  # .venv 생성 + 의존성 설치 (uv.lock 기준)
uv run python -m pytest  # 테스트 실행 (CI와 동일)
```

- Python 3.12 (`.python-version`), 의존성 단일 출처는 `pyproject.toml` + `uv.lock`
- 필수 환경 변수는 `.env.example` 참조
- Feast 작업은 격리 그룹 사용: `uv sync --only-group feast`

## 문서

- 문서 인덱스: [`docs/README.md`](docs/README.md)
- 기여 규칙(브랜치·이슈·PR 전략): [`CONTRIBUTING.md`](CONTRIBUTING.md)
- AI 코딩 에이전트 가이드: [`CLAUDE.md`](CLAUDE.md)
