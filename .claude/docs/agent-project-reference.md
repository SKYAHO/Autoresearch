# Agent Project Reference

> Last Updated: 2026-07-24

폴더별 책임과 팀 소유권 경계를 찾기 위한 문서입니다. "새 코드를 어디에
두는가?", "Y는 누가 소유하는가?" 질문에 답합니다. 디렉토리 구조 지도와 배포
이미지 목록의 정본은 `README.md`이며 여기에 복제하지 않습니다.

## When To Use This Doc

- 새 코드를 추가할 위치를 정해야 할 때
- 팀 도메인 경계와 소유권을 확인해야 할 때
- 폴더 간 책임 경계(무엇을 소유하지 않는지)를 확인해야 할 때

## Docs Layout

```
docs/
├── README.md                # 문서 인덱스·수명 규칙의 정본
├── adr/                     # Architecture Decision Records (영구)
├── specs/                   # 살아있는 계약·설계 spec (유효한 동안)
├── plans/                   # 진행 중 구현 계획 (완료 시 archive로)
├── guides/                  # 운영·아키텍처 가이드 (상시 갱신)
├── runbooks/                # 운영 절차·트러블슈팅 기록 (상시 갱신)
├── reports/                 # 팀 공유용 시각화 리포트 (HTML)
└── archive/                 # 완료·과거 문서 보존 (수정하지 않음)
```

- 새 spec/plan은 `docs/specs/`, `docs/plans/`에 `YYYY-MM-DD-<slug>.md`로
  만들고, 구현이 완료되어 더 이상 계약으로 쓰이지 않으면 `docs/archive/`로
  옮깁니다.
- 코드 디렉토리 안에 문서를 두지 않습니다(모듈 사용법은 `docs/guides/`).

## Team Ownership & Domains

| 도메인 | 팀원 | 책임 | 주요 경로 |
|---|---|---|---|
| **Model Training** | waieiches, hyochangsung | 모델 구조, 학습 파이프라인, 평가 지표, MLflow 연동 | `src/models/`, `src/pipeline/`, `src/tracking/` |
| **Feast Features** | waieiches, hyochangsung | 피처 정의, 피처 엔지니어링, 피처 스토어 연동 | `feature_repo/`, `src/features/` |
| **YouTube Collection & Release** | Noah-JuYong | YouTube 수집 파이프라인·복원력 레이어·프록시, release/배포 자동화 워크플로우 | `autoresearch/youtube_collection/`, `proxy/`, `.github/workflows/` |
| **Airflow Orchestration** | bbungjun | DAG 정의, 스케줄링, 오케스트레이션 | `SKYAHO/Autoresearch-airflow` |
| **GCP Infrastructure** | hyeongyu-data | 클라우드·Kubernetes 리소스, IAM, 시크릿 기반 | `SKYAHO/Autoresearch-infra` |
| **Agent Orchestration** | (미지정) | FastAPI 채팅 저장 API, Codex CLI/OpenAI 호출, PostgreSQL 저장 | `agent_orchestration/` |

> `src/serving/`(리랭킹 API)과 정책 라운드·일일 추천 폐루프의 도메인 소유는
> 미지정입니다(#149 구조 논의에서 확정 예정). 해당 영역 변경은 팀 확인 후
> 진행합니다.

## Ownership Boundaries

### `autoresearch/youtube_collection/`
- **책임:** YouTube API 수집, 변환, GCS 적재, 백필. 복원력 레이어
  (`client.py`: 재시도/Key 롤링/IP밴 시그니처/프록시)를 포함합니다.
- **패턴:** fetch → transform → load 단계를 파일로 분리합니다. 데이터
  계약은 `schema.py`의 pydantic 모델로 정의합니다.

### `autoresearch/virtual_users/`
- **책임:** 페르소나 원천 데이터 로드, LLM 기반 가상 유저 생성
- **패턴:** 외부 API 호출과 오케스트레이션(`pipeline.py`)을 분리합니다.

### `autoresearch/action_logs/`와 `autoresearch/jobs/`
- **책임:** action log 도메인 로직과 Airflow 비종속 공개 batch 계약을
  소유합니다. `autoresearch/action_logs/`는 BigQuery 비의존 순수 모듈로
  유지합니다(BQ 리더는 `src/pipeline/`).
- **경계:** `jobs/`는 입력을 검증하고 도메인 모듈을 호출하지만 schedule,
  retry, timeout, Pool과 KubernetesPodOperator 설정은 소유하지 않습니다.

### `src/` (CTR 학습·서빙 파이프라인)
- **책임:** 피처 조립(`features/`), 학습·평가·학습 데이터셋·일일 추천·정책
  시뮬레이션(`pipeline/`), LightGBM 모델(`models/`), MLflow
  tracking/registry(`tracking/`), FastAPI 리랭킹 추론 서버(`serving/`).
- **경계:** `src/serving/`은 온라인 추론만 담당하며 배치 파이프라인을
  import하지 않습니다. 피처 온라인 조회는 Feast(`feature_repo/`) 경유.
- **참고:** `src/` → `autoresearch/` 패키지 통합이 논의 중입니다
  (`docs/specs/2026-07-15-repo-restructure.md` 결정 3, 팀 합의 대기).
  통합 전까지 신규 학습·서빙 코드는 기존 `src/` 배치를 따릅니다.

### `agent_orchestration/`
- **책임:** 실험형 오케스트레이션 API와 비공개 Codex Runner. `/chat`의
  프롬프트 처리·PostgreSQL 영속화, API→Runner 내부 토큰 계약을 제공한다.
- **배포 경계:** `deploy/agent_orchestration/api.Dockerfile`은 DB·API만,
  `runner.Dockerfile`은 Codex CLI·OAuth PVC만, `ui.Dockerfile`은 Streamlit UI와
  Experiment API 표시 모델만 소유한다. KSA/GSA·PVC·NetworkPolicy는
  `SKYAHO/Autoresearch-infra` 소유이다.
- **비책임:** 사용자 OAuth, 세션/사용자 히스토리, 정책 라우팅은 후속 단계다.
- **패턴:** `src/`와 패키지 경계를 분리해 새로운 배포 단위를 별도로 둔다.
- **이슈 발행 환경 변수(#516):** 가설을 `[AR]` 이슈로 발행하는 경로가 쓰는
  필수 환경 변수. 전체 기본값·형식은 `.env.example`이 정본.
  - `ORCH_GITHUB_TOKEN`: 이슈 발행 전용 `issues: write` GitHub 토큰.
  - `ORCH_GITHUB_REPOSITORY`: 발행 대상 저장소(`owner/repo`), 발행 결과 URL과
    대조해 오발행을 막음.
  - `ORCH_GH_TIMEOUT_SEC`: `gh` 서브프로세스 실행 상한(초).
  - `ORCH_ISSUE_DAILY_LIMIT`: 일일 발행 상한, 초과 시 429 반환.
  - `ORCH_EXPERIMENT_DATASET_SNAPSHOT`: 서버가 Issue Form에 채우는 데이터셋
    스냅샷.
  - `ORCH_EXPERIMENT_TRAINING_CONFIG_REF`: 서버가 Issue Form에 채우는 학습
    설정 참조.
  - `ORCH_EXPERIMENT_DATASET_WINDOW`: 서버가 Issue Form에 채우는 대상
    데이터·기간.

### 외부 오케스트레이션 경계
- DAG와 Airflow 배포는 `Autoresearch-airflow`에만 둡니다.
- Airflow는 배포 이미지의 immutable digest와 `autoresearch.jobs.*` 공개
  명령만 소비하며 내부 Python API를 직접 import하지 않습니다.
- 공개 batch 명령·인자 계약:
  `docs/specs/2026-07-13-public-batch-execution-contract.md`
- 모델 승격 판정과 `model-promotion-result-v1` schema는 이 저장소가
  소유합니다. Airflow는 `--result-path` 파일을 XCom으로 운반하고 알림으로
  렌더링하지만 `src.tracking` 내부 API를 import하거나 outcome을 다시
  판정하지 않습니다. 결과 정본:
  `docs/specs/2026-07-29-model-promotion-structured-outcome.md`
- paired offline 실험(#454)의 비교·판정도 이 저장소가 소유합니다. Airflow는
  조건별(baseline|candidate) 학습 Job을 실행하고
  `python -m src.cli compare-paired-experiment`의 결과 파일을 운반할 뿐,
  `comparison_passed`/`comparison_rejected`/`comparison_failed` 판정을 다시
  계산하지 않습니다. 실행 좌표는 `autoresearch/experiments/context.py`가,
  결과 계약은 `src/pipeline/paired_experiment.py`가 소유하며 정본은
  `docs/specs/2026-08-03-paired-offline-experiment-comparison.md`입니다.

### `tests/`
- **책임:** 모듈별 단위 테스트. `tests/test_<module>.py` 플랫 구조를
  따릅니다. 새 모듈에는 대응하는 테스트 파일을 만듭니다.
- feast 계열 테스트는 dev 환경에서 `pytest.importorskip("feast")`로 skip되고
  CI `pytest (feast group)` job이 별도 실행합니다.

## Technical Stack

- **언어:** Python 3.12 (`.python-version`), CI는 3.11/3.12 매트릭스
- **의존성:** uv + `pyproject.toml`/`uv.lock`(단일 출처).
  `proxy/requirements.txt`는 `uv export` 전핀 산출물,
  `deploy/mlflow/runtime`은 자체 lock — CI가 drift를 검사합니다.
- **주요 라이브러리:** pydantic v2, pyarrow, pandas, DuckDB, LightGBM,
  scikit-learn, typer, mlflow-skinny, google-cloud-bigquery/storage/aiplatform,
  openai(OpenRouter 호출)
- **데이터 저장:** GCS 데이터 레이크(parquet), BigQuery(피처·학습 데이터셋
  운영 중)
- **피처 스토어:** Feast 0.64 (`feature_repo/`, BigQuery offline / Redis
  online) — dev와 의존성 충돌로 격리 그룹(`uv sync --only-group feast`).
  prod/dev 환경은 `AUTORESEARCH_ENV`(기본 prod)로 선택한다. dev는 오프라인 전용
  (apply + BigQuery PIT)이라 registry(`GCS_REGISTRY_PATH`)·offline(`BQ_DATASET`)만
  분리하고 online 서빙·materialize는 prod만 한다. dev apply는
  `full_scan_for_deletion=false`(`feature_repo/env.py`)로 Redis에 접속하지 않는다
  (#399, `docs/specs/2026-07-29-feature-store-prod-dev-environment.md`)
- **모델·추적:** LightGBM + MLflow (Tracking Server는 `deploy/mlflow/`)
- **서빙:** FastAPI (`src/serving/`), GKE 배포(`deploy/serving/`)
- **오케스트레이션:** 외부 `Autoresearch-airflow`가 배포 이미지의 공개
  CLI를 KubernetesPodOperator로 실행

## Key Extension Rules

1. **도메인 소유권 확인:** 애플리케이션·ML은 이 저장소, Airflow와 GCP
   인프라는 각각 전용 저장소에서 변경합니다.
2. **올바른 위치에 배치:** 위 책임 경계를 따르고 도메인 간 결합을
   피합니다.
3. **데이터 계약 갱신:** 스키마가 바뀌면 해당 모듈의 `schema.py`
   pydantic 모델과 테스트를 함께 수정합니다.
4. **테스트 작성:** `tests/test_<module>.py`에 단위 테스트를 추가합니다.
5. **설계 결정 기록:** 아키텍처에 영향이 있으면 `docs/specs/`에 spec을
   남기거나 관련 `.claude/docs/` 가이드를 갱신합니다.
6. **구조 사실 갱신:** 새 최상위 디렉토리·`Dockerfile.*`·공개 CLI·필수 환경
   변수를 도입하면 같은 PR에서 `README.md`와 이 문서를 갱신합니다.

## Verification Checklist

- [ ] 코드가 팀 도메인에 맞는 폴더에 있다.
- [ ] 공개 CLI에 schedule·retry·KPO 같은 Airflow 정책이 들어가지 않았다.
- [ ] 스키마 변경 시 pydantic 모델과 테스트를 함께 수정했다.
- [ ] 새 기능에 테스트가 있다.
- [ ] 동작·설정이 바뀌었으면 문서를 갱신했다(구조 사실은 README와 이 문서).
