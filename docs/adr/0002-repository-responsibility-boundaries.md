# ADR 0002: 애플리케이션·Airflow·인프라 저장소의 책임 경계

- **상태**: Accepted
- **날짜**: 2026-07-13
- **이슈**: #125
- **실행 계약**: `docs/specs/2026-07-13-public-batch-execution-contract.md`

## 배경

Autoresearch 프로젝트는 애플리케이션·ML 코드, Airflow 오케스트레이션,
GCP·Kubernetes 인프라를 세 저장소로 분리하고 있다. 결정 당시에는 다음과 같이
책임이 겹쳤다.

- `Autoresearch`에 Airflow DAG, Astro Dockerfile, `airflow_settings.yaml`이 있다.
- `Autoresearch-airflow`의 batch entrypoint가 CLI 변환을 넘어 GCS 경로 계산,
  입력 검증, 결과 삭제, idempotency, telemetry 필터와 데이터 품질 검사를
  구현한다.
- `Autoresearch-airflow`가 `Autoresearch`를 빌드 중 clone하여 application batch
  image를 만든다.
- Airflow DAG는 git-sync로 갱신되지만 DAG helper는 Airflow image에 포함되어
  두 버전이 일시적으로 어긋날 수 있다.
- Airflow 운영 문서 일부가 namespace, Workload Identity, Secret 생성까지
  설명해 인프라 저장소와 실행 책임이 겹친다.

이 구조에서는 같은 동작을 두 저장소에서 수정하거나, 한 저장소의 변경을
배포하기 위해 다른 저장소의 빌드가 필요하다. 특히 현재 운영 중인 action-log
DAG는 애플리케이션 commit, Airflow DAG commit, Airflow helper image가 함께
맞아야 하므로 독립적인 release와 rollback이 어렵다.

## 결정

다음 원칙으로 저장소 책임을 고정한다.

> `Autoresearch`는 무엇을 실행하는가, `Autoresearch-airflow`는 언제·어떤
> 순서로 실행하는가, `Autoresearch-infra`는 어디에서 실행하는가를 담당한다.

### 1. `Autoresearch`: 애플리케이션·ML 도메인

`Autoresearch`는 실행 가능한 애플리케이션 동작과 데이터 계약의 단일 원본이다.

포함한다:

- YouTube 수집, virtual user, action-log 생성 로직
- 입력·출력 schema와 데이터 품질 규칙
- shard/checkpoint/merge/idempotency 동작
- Feature Store, 학습·평가, MLflow 연동
- FastAPI 추론 서버
- Airflow와 로컬 실행에서 공통으로 사용할 공개 batch CLI
- application·batch·serving image 정의와 release
- 애플리케이션 단위·통합·image smoke test

제외한다:

- Airflow DAG, Sensor, Operator, schedule, Pool, retry와 task timeout
- Airflow Helm chart와 values
- GCP·Kubernetes resource 생성
- 다른 저장소 source를 clone하여 image를 조립하는 빌드

### 2. `Autoresearch-airflow`: 워크플로 오케스트레이션·Airflow 배포

`Autoresearch-airflow`는 공개 batch CLI를 호출하고 task 실행 정책을 관리한다.

포함한다:

- DAG ID, schedule, catchup, task dependency
- Sensor와 KubernetesPodOperator 설정
- Airflow retry, timeout, Pool, concurrency와 trigger rule
- Airflow Variable·Connection·Secret reference를 CLI 인자와 환경 변수로 연결
- 운영·QA DAG와 Airflow 전용 안전 정책
- Airflow Helm chart, values와 Airflow release runbook
- DAG parse, KPO argument, Helm render test

제외한다:

- action-log, 수집, Feature Store, 학습·평가의 핵심 로직
- 데이터 schema와 도메인 품질 규칙의 재구현
- application batch image 빌드
- Terraform과 GCP·Kubernetes 기반 resource 생성

이 ADR에서 말하는 "얇은 batch entrypoint"는 KPO의 `cmds`, `arguments`, 환경
변수 매핑 같은 호출 adapter를 뜻한다. 별도 Python wrapper를 유지해야 한다면
공개 CLI에 인자를 그대로 전달하는 delegation만 허용하며, 경로 계산, GCS I/O,
도메인 기본값, 성공 판정과 결과 삭제를 포함하지 않는다.

### 3. `Autoresearch-infra`: 클라우드·Kubernetes 기반 인프라

`Autoresearch-infra`는 두 workload가 실행될 기반 resource와 접근 권한을
관리한다.

포함한다:

- GKE, node pool, GAR, GCS, BigQuery, Cloud SQL
- IAM, Workload Identity Federation, service account
- namespace, RBAC, NetworkPolicy와 고정 IP
- Secret Manager와 Kubernetes Secret 연동 기반
- Terraform state, module, environment variable와 output

제외한다:

- DAG와 Airflow task 동작
- 애플리케이션·ML 코드
- application 또는 Airflow image의 Docker build 정의

Airflow Helm values는 `Autoresearch-airflow`가 소유한다. values는 infra output과
Secret reference를 소비할 수 있지만 resource를 직접 생성하거나 Terraform
상태를 대체하지 않는다.

## 허용 의존 방향

```text
Autoresearch release ── application image digest ──> Autoresearch-airflow
Autoresearch-infra ── resource output/reference ────> Autoresearch-airflow
Autoresearch-infra ── GAR·WIF·runtime resource ─────> Autoresearch
Autoresearch-airflow ── public CLI invocation ──────> Autoresearch image
```

application release는 검증된 digest를 전달하기 위해 Airflow Helm values만 바꾸는
PR을 자동 생성할 수 있다. 이는 배포 설정의 소유권을 이전하는 것이 아니다.
PR merge와 이후 Helm 배포는 계속 `Autoresearch-airflow`의 보호 규칙과 workflow가
통제한다.

다음 역방향 의존은 금지한다.

- `Autoresearch`가 Airflow package나 DAG module을 import하는 것
- `Autoresearch-airflow`가 `autoresearch.action_logs` 등 내부 Python API를
  직접 import하는 것
- Airflow image build가 `Autoresearch` source를 clone하거나 복사하는 것
- `Autoresearch-infra`가 DAG, 애플리케이션 source 또는 image build step을
  포함하는 것

## 설정 소유권

| 설정 종류 | 단일 원본 | 소비자 책임 |
| --- | --- | --- |
| CLI 명령, 인자, type, 의미 | `Autoresearch` | Airflow는 값을 선택해 전달 |
| schema, CTR, 후보·shard 의미 | `Autoresearch` | Airflow는 재구현하지 않음 |
| schedule, retry, timeout, Pool | `Autoresearch-airflow` | app은 인식하지 않음 |
| 운영용 파라미터 값 | `Autoresearch-airflow` | app의 허용 범위 안에서 선택 |
| bucket, cluster, service account | `Autoresearch-infra` | Helm·KPO가 output/reference 소비 |
| API key와 credential 원문 | Secret Manager 계층 | Airflow는 reference만 연결 |
| application image digest | `Autoresearch` release | Airflow가 immutable digest로 고정 |

애플리케이션은 Airflow가 잘못된 값을 전달할 가능성을 전제로 모든 입력을 다시
검증한다. Airflow는 QA namespace 제한처럼 애플리케이션보다 더 엄격한 운영
정책을 적용할 수 있지만 도메인 허용 범위를 넓힐 수 없다.

## 문서의 단일 원본

이 ADR과 연결된 공개 실행 계약을 교차 저장소 경계의 단일 원본으로 사용한다.

- 책임 결정: `docs/adr/0002-repository-responsibility-boundaries.md`
- 실행 계약: `docs/specs/2026-07-13-public-batch-execution-contract.md`

`Autoresearch-airflow`와 `Autoresearch-infra`에는 각 저장소 운영에 필요한 내용만
두고 위 문서에 링크한다. 책임 표와 CLI 계약 전문을 복사하지 않는다.

## 결정 당시 파일의 처분과 구현 상태

### `Autoresearch`

| 결정 당시 파일 | 최종 상태 |
| --- | --- |
| `autoresearch/action_logs/` | 유지: 도메인 단일 원본 |
| `autoresearch/youtube_collection/` | 유지: 수집 단일 원본 |
| `dags/youtube_action_log_daily.py` | 통합 DAG 전환·canary 뒤 제거 완료 |
| `dags/youtube_trending_kr_daily.py` | 공개 CLI 기반 KPO 전환 뒤 제거 완료 |
| `dags/youtube_backfill_kr.py` | public backfill CLI·KPO smoke 뒤 제거 완료 |
| `airflow_settings.yaml` | Airflow 저장소 전환 뒤 제거 완료 |
| Astro용 root `Dockerfile`, `packages.txt`, `requirements.txt` | 제거 완료 |
| `Dockerfile.app` | canonical application batch image로 유지 |
| Airflow DAG 전용 test | 대체 DAG 검증 뒤 제거 완료 |

### `Autoresearch-airflow`

| 현재 파일 | 처분 |
| --- | --- |
| `dags/` | 유지: DAG와 KPO 단일 원본 |
| `autoresearch_airflow/dag_config.py` | git-sync되는 DAG subtree로 이동 |
| `autoresearch_airflow_jobs/daily_action_log.py` | app 공개 CLI 전환 후 제거 |
| `autoresearch_airflow_jobs/daily_youtube_trending.py` | app 공개 CLI 전환 후 제거 |
| `scripts/check_action_log_data_quality.py` | `Autoresearch`로 이동 |
| `docker/batch/Dockerfile` | app image 전환 후 제거 |
| batch image build workflow | app release 전환 후 제거 |
| `docker/airflow/Dockerfile` | Airflow 전용 의존성이 있을 때만 유지 |
| `charts/`, `helm/` | 유지: Airflow 배포 단일 원본 |

## 전환 원칙

운영 중인 DAG를 중단하지 않기 위해 항상 `추가 → 전환 → 삭제` 순서를 따른다.

1. `Autoresearch`에 공개 CLI와 canonical batch image를 추가한다.
2. 기존 Airflow wrapper를 새 CLI delegation으로 바꿔 동작 parity를 검증한다.
3. Airflow KPO가 canonical image digest와 공개 CLI를 직접 호출하게 한다.
4. QA DAG로 shard, checkpoint, merge와 데이터 품질을 검증한다.
5. 이전 DAG를 pause하고 새 DAG의 scheduled run을 관찰한다.
6. 호환 wrapper, 중복 image build와 기존 Astro 표면을 제거한다.

각 단계는 독립 issue와 PR로 진행한다. 한 PR에서 두 저장소를 동시에 깨뜨리는
breaking change를 만들지 않는다.

## 결과

- 애플리케이션은 Airflow 없이 로컬·CI·KPO에서 같은 명령으로 실행된다.
- Airflow 변경은 애플리케이션 source를 빌드하지 않고 image digest만 선택한다.
- DAG와 helper는 같은 git-sync revision으로 배포된다.
- 인프라 변경은 application·DAG release와 독립적으로 plan·apply할 수 있다.
- 각 저장소는 자체 테스트와 rollback 단위를 갖는다.
- 모든 action-log shard는 동일한 데이터 작업만 수행하며 shard 0에 기존 최종
  결과 삭제 같은 별도 운영 역할을 부여하지 않는다.
- 상세 격리 파일은 선택적 진단 산출물로 취급하고, 새 최종 parquet이 완성되기
  전까지 이전 정상 parquet을 마지막 정상 결과로 유지한다.

## 채택 근거

2026-07-13에 다음 조건을 확인해 `Accepted`로 변경했다. 실행·canary·rollback
근거는 `docs/specs/2026-07-13-autoresearch-airflow-boundary-cutover.md`에 기록한다.

- Application·Airflow·Infrastructure 담당자가 책임 경계를 검토한다.
- 공개 실행 계약의 명령·입출력·오류 의미에 합의한다.
- 후속 구현 issue가 저장소별로 분리될 수 있을 만큼 이전 대상이 명확하다.

## 참고

- `Autoresearch-airflow`: https://github.com/SKYAHO/Autoresearch-airflow
- `Autoresearch-infra`: https://github.com/SKYAHO/Autoresearch-infra
- `Autoresearch` 제거 대상 레거시 DAG: `youtube_action_log_daily`
- `Autoresearch-airflow` 현재 운영 DAG: `youtube_gcs_action_log_pipeline`
