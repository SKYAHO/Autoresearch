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
agent_orchestration/  # 실험형 FastAPI 채팅 API + PostgreSQL 저장
src/                 # CTR 학습·서빙 파이프라인
├── features/             # 피처 엔지니어링·조립
├── models/               # LightGBM 모델
├── pipeline/             # 학습·평가·학습 데이터셋·일일 추천·정책 시뮬레이션
├── serving/              # FastAPI 리랭킹 추론 서버
├── tracking/             # MLflow tracking·registry 연동
└── utils/                # 모델 저장/로드 유틸리티
proxy/               # Cloud Run dumb forwarder (YouTube API IP밴 대응)
deploy/              # 배포 산출물 (mlflow/ Tracking Server, serving/ 추론 이미지,
                     #             agent_orchestration/ 역할별 runtime 이미지,
                     #             feast/ feast apply GKE Job 매니페스트, 롤백용)
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
| `Dockerfile.train` | feast 불필요 학습 서브커맨드 — `promote-model`(alias 승격), `train-model`/`evaluate-model`/`sweep-seeds`(다중 시드 반복 학습·유의성 판정 근거, #407), `compare-paired-experiment`(baseline/candidate paired 비교·판정, #454), `measure-degradation`(단일 cutoff 기반 모델 열화 시점 측정, #471/#485). `train-model --dataset-uri`(게시된 학습 데이터셋 스냅샷 재사용, #530)는 GCS 다운로드만 필요해 이 이미지로 실행 가능하다. GCS code archive 부트스트랩, MLflow 연동 |
| `Dockerfile.feast` | Feast apply/materialize + feast 필요 학습 조립 — `build-features`/`run-pipeline`이 offline PIT로 피처를 조립하므로(#359 C2) 이 이미지로 실행. `--snapshot-root`(또는 `TRAINING_SNAPSHOT_ROOT`)로 조립 결과를 GCS에 content-addressed 게시할 수 있다(#530, `docs/guides/training-dataset.md`) |
| `deploy/serving/Dockerfile` | 리랭킹 서빙 API (GKE) |
| `deploy/mlflow/Dockerfile` | MLflow Tracking Server |
| `deploy/agent_orchestration/api.Dockerfile` | Agent Orchestration FastAPI·PostgreSQL 저장 API (GKE 내부) |
| `deploy/agent_orchestration/runner.Dockerfile` | API 전용 Codex Runner (GKE 내부, OAuth PVC 분리) |
| `deploy/agent_orchestration/ui.Dockerfile` | Streamlit Experiment Workbench (GKE 내부, API 토큰 서버 환경 주입) |
| `deploy/agent_orchestration/launcher.Dockerfile` | 봉인 좌표를 선점해 branch-bootstrap Kubernetes Job을 생성하는 1회 launcher runtime (CronJob용) |
| `deploy/agent_orchestration/executor.Dockerfile` | Phase 2 GitHub App token-minter, 봉인 issue/workspace, Codex, verifier, candidate finalizer를 같은 digest로 실행하는 executor runtime |

`release.yml`은 launcher와 executor를 각각
`autoresearch-agent-orchestration-launcher`,
`autoresearch-agent-orchestration-executor`로 build/push합니다. 배포 인프라는 tag가
아니라 release가 검증한 `@sha256:<64자리 digest>`를 소비합니다.

DAG·스케줄·Airflow 배포는 [`SKYAHO/Autoresearch-airflow`](https://github.com/SKYAHO/Autoresearch-airflow),
GCP 인프라는 [`SKYAHO/Autoresearch-infra`](https://github.com/SKYAHO/Autoresearch-infra)가 소유합니다.

### Agent Orchestration 이슈 발행 환경 변수 (#516)

가설을 `[AR]` Auto Research 이슈로 발행하는 경로가 쓰는 필수 환경 변수입니다
(전체 기본값·형식은 `.env.example`이 정본).

| 변수 | 용도 |
|---|---|
| `ORCH_GITHUB_TOKEN` | 이슈 발행 전용 `issues: write` GitHub 토큰 |
| `ORCH_GITHUB_REPOSITORY` | 발행 대상 저장소(`owner/repo`), 발행 결과 URL과 대조해 오발행을 막음 |
| `ORCH_BASELINE_GITHUB_APP_ID` | 이슈 발행 전에 `heads/dev`를 읽는 Contents read 전용 GitHub App ID |
| `ORCH_BASELINE_GITHUB_APP_INSTALLATION_ID` | baseline reader App installation ID |
| `ORCH_BASELINE_GITHUB_APP_PRIVATE_KEY_PATH` | API Pod에 read-only mount한 baseline reader private key 파일 경로 |
| `ORCH_GH_TIMEOUT_SEC` | `gh` 서브프로세스 실행 상한(초) |
| `ORCH_ISSUE_DAILY_LIMIT` | 일일 발행 상한, 초과 시 429 반환 |
| `ORCH_EXPERIMENT_DATASET_SOURCE` | 서버가 Issue Form에 채우는 학습 데이터 출처 좌표. 기간은 발행 시점에 서버가 계산해 붙임(`dt BETWEEN P-30 AND P-1`, 어제까지 30일) |
| `ORCH_EXPERIMENT_TRAINING_CONFIG_REF` | 서버가 Issue Form에 채우는 학습 설정 참조 |

### Agent Orchestration 실험 executor Job handoff (#557)

release는 launcher/executor/API를 각각 `@sha256:<64자리 digest>`로 게시하고, Infra는
그 digest만 배포 입력으로 사용합니다. launcher는 DB에서 `CREATED` Experiment를 선점해
`RUNNING`으로 전이한 뒤, 아래 정확한 값과 volume 경로를 executor Job에 전달합니다.
값·기본값의 단일 출처는 `.env.example`입니다.

| 역할 | 변수 | 용도 |
|---|---|---|
| launcher | `ORCH_DATABASE_URL` | Experiment 선점·생성 확인을 기록할 PostgreSQL 연결 |
| launcher | `ORCH_JOB_NAMESPACE` | branch-bootstrap Job 생성 namespace |
| launcher | `ORCH_EXECUTOR_IMAGE` | release가 게시한 executor `@sha256:` digest reference |
| launcher | `ORCH_EXECUTOR_SERVICE_ACCOUNT` | Kubernetes API 권한이 없는 executor KSA |
| launcher | `ORCH_EXECUTOR_NODE_POOL` | executor Job의 nodeSelector·toleration 좌표 |
| launcher | `ORCH_GITHUB_APP_SECRET_NAME` | token-minter에만 mount할 branch-writer App Secret 이름 |
| launcher/token-minter | `ORCH_GITHUB_APP_ID`, `ORCH_GITHUB_APP_INSTALLATION_ID` | Contents write 전용 branch-writer App 공개 좌표 |
| launcher | `ORCH_MAX_CONCURRENT_EXPERIMENTS` | namespace의 branch-bootstrap Job 동시 실행 상한 |
| launcher | `ORCH_CODEX_HOME_SECRET_NAME` | Infra가 생성·이름을 소유하는 executor 전용 Codex 인증 Secret 이름 (`auth.json` key 제공, launcher가 volume `defaultMode=0440` 지정) |
| launcher | `ORCH_ACTIVE_DEADLINE_SEC` | 8-container Job 전체 실행 상한 (`3600`초) |
| launcher | `ORCH_TTL_AFTER_FINISHED_SEC` | 완료 Job 보존 시간(기본 `30`초, 장애 smoke에서만 일시 조정) |
| launcher | `ORCH_MLFLOW_TRACKING_URI` | 학습이 MLflow run을 기록할 tracking server 좌표. executor에는 접두사 없는 `MLFLOW_TRACKING_URI`로 내보낸다 |
| executor | `ORCH_EXPERIMENT_ID`, `ORCH_ISSUE_NUMBER`, `ORCH_ISSUE_BRANCH`, `ORCH_BASE_DEV_SHA` | launcher가 DB에서 복사해 전달하는 불변 branch 좌표 |
| token-minter | `ORCH_GITHUB_APP_PRIVATE_KEY_FILE` | branch/clone/push token-minter에만 보이는 private key mount 경로 |
| token-minter/각 consumer | `ORCH_GITHUB_TOKEN_FILE` | purpose별 memory volume의 mode 0400 installation token 파일 경로 (`/var/run/{branch,clone,push}-token/token`) |
| candidate-finalizer | `ORCH_EXECUTOR_API_URL`, `ORCH_EXECUTOR_API_TOKEN_FILE` | internal Candidate API URL과 `ORCH_EXECUTOR_API_TOKEN` Secret을 mount한 `/var/run/executor-api-token/token` 경로 |
| codex-worker | `ORCH_CODEX_HOME`, `ORCH_CODEX_TIMEOUT_SEC` | read-only Codex auth source와 Job 전체 상한보다 작은 Codex 실행 상한 (`1800`초) |

동일 executor digest는 아래 8개 container가 순서대로 사용합니다. GitHub App private key는
1·3·7의 token-minter에만, executor 전용 Codex 인증 Secret의 `CODEX_HOME`은 5에만, `ORCH_EXECUTOR_API_TOKEN`은
8에만 mount합니다. 5·6에는 GitHub/API credential volume을 mount하지 않습니다.

1. `branch-token-minter`: private key → branch token memory volume
2. `branch-creator`: branch token → 봉인 `base_dev_sha`의 exp ref 관찰/생성
3. `clone-token-minter`: private key → clone token memory volume
4. `workspace-preparer`: clone token + issue 번호·branch·기준 SHA → raw issue 조회·workspace/state
5. `codex-worker`: workspace + read-only `.git` + state + read-only auth source `CODEX_HOME` →
   `/tmp` 아래 mode 0700 per-run writable scratch `CODEX_HOME`에 regular `auth.json`만 mode 0400으로
   복사 → `codex exec --ephemeral`으로 working tree 수정. config·plugin 등 다른 source 파일은
   복사하지 않음
6. `candidate-verifier`: workspace + read-only `.git` + state → 고정 Ruff/pytest 검증 결과
7. `push-token-minter`: private key → push token memory volume
8. `candidate-finalizer`: workspace + push token + verifier 결과 + API token → candidate commit/push, `candidate_sha` 저장, `RUNNING → EVALUATING`

executor image에는 Git CLI, uv, `/opt/autoresearch-venv`의 lock 기반 기본+`dev`
의존성(Feast 제외), Node.js, `@openai/codex@0.146.0`과 `UV_PROJECT_ENVIRONMENT`가
고정됩니다. workspace-preparer는 runtime clone의 Issue Form parser를 실행하지 않고 GitHub의
현재 이슈 본문을 raw 입력으로 전달합니다. repository 소스 전체, `.env`, `auth.json`, Codex
인증은 image에 포함하지 않으며 Codex 인증은 runtime mount로만 제공합니다.

Infra companion PR에는 다음을 확인 항목으로 옮깁니다. 실제 Secret/PVC/resource/
NetworkPolicy 이름·값은 `Autoresearch-infra` 소유이므로 이 저장소에서 단정하지 않습니다.

- GitHub App private key를 branch/clone/push token-minter에만 mount
- executor 전용 Codex 인증 Secret의 `auth.json` key를 launcher가 mode 0440의 read-only
  `subPath` 파일로 codex-worker에만 mount하고, writable `executor-tmp`의 `/tmp`를 per-run
  scratch에 제공
- `ORCH_EXECUTOR_API_TOKEN`을 candidate-finalizer에만 mount
- workspace/token `emptyDir` size limit과 GitHub·OpenAI·internal API 최소 egress
- immutable launcher/executor/API digest, non-root/seccomp/capability drop/
  `automountServiceAccountToken=false`

`auto-experiment`는 `[AR]` 이슈의 분류와 promotion guard에 남지만 branch 생성
트리거가 아닙니다. Phase 1 executor는 기존 GitHub Actions bot marker를 새로 쓰지
않으므로, 새 marker 없는 exp branch는 promotion workflow 입력이 아닙니다. marker
신뢰 계약 재설계는 실제 실험 실행 전 후속 gate입니다.

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
