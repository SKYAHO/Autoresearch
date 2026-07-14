# Autoresearch–Airflow 책임 경계 전환 명세

- **상태**: Proposed
- **날짜**: 2026-07-13
- **관련 이슈**: #125, #129
- **관련 ADR**: `docs/adr/0002-repository-responsibility-boundaries.md`
- **공개 실행 계약**: `docs/specs/2026-07-13-public-batch-execution-contract.md`

## 1. 목적

이 문서는 `Autoresearch`와 `Autoresearch-airflow` 사이에 남아 있는 구현·빌드·배포
결합을 제거하고, YouTube 수집부터 action-log 최종 검증까지의 일일 파이프라인을
독립적으로 릴리스하고 빠르게 검증할 수 있는 구조로 전환하기 위한 구현 명세다.

책임 결정과 CLI의 상세 의미는 관련 ADR과 공개 실행 계약을 단일 원본으로 사용한다.
이 문서는 해당 결정을 현재 저장소 구조에 적용하는 순서, 호환성 보완, 검증 게이트와
삭제 조건만 정의한다.

핵심 목표는 다음과 같다.

> `Autoresearch`는 실행 가능한 batch 동작과 image를 제공하고,
> `Autoresearch-airflow`는 검증된 image digest의 공개 CLI를 언제·어떤 순서로
> 실행할지만 결정한다.

## 2. 성공 정의

전환 완료 후 일일 파이프라인은 다음 경계로 실행된다.

```text
Autoresearch merge
  → application batch image build·push
  → immutable image digest 발행
  → Airflow QA DAG가 digest 선택
  → YouTube collect KPO
  → action-log shard KPO × N
  → action-log merge KPO
  → action-log quality KPO
  → production digest 승격
```

각 단계의 소유권은 다음과 같다.

| 단계 | 소유 저장소 | 공개 경계 |
| --- | --- | --- |
| YouTube 수집·적재 | `Autoresearch` | `autoresearch.jobs.youtube_trending` |
| action-log shard·merge | `Autoresearch` | `autoresearch.jobs.action_log` |
| action-log 품질 판정 | `Autoresearch` | `autoresearch.jobs.action_log_quality` |
| schedule·fan-out·fan-in·retry | `Autoresearch-airflow` | DAG/KPO 설정 |
| application image 선택 | `Autoresearch-airflow` | immutable digest |
| GAR·WIF·GKE·GCS·IAM | `Autoresearch-infra` | resource output/reference |

## 3. 현재 상태와 문제

작성 시점의 저장소에는 다음 결합이 남아 있다.

1. `Autoresearch-airflow` KPO는 공개 application CLI 대신
   `autoresearch_airflow_jobs.daily_youtube_trending`과
   `autoresearch_airflow_jobs.daily_action_log`을 실행한다.
2. Airflow action-log wrapper가 `autoresearch.action_logs.daily` 내부 함수를 직접
   import하고 GCS 경로 기본값, 입력 존재 검사, 결과 삭제, skip/overwrite와
   telemetry 필터를 구현한다.
3. Airflow merge wrapper는 현재 application merge 함수가 받지 않는
   `quarantine_base_path`, `shard_quarantine_base_path` 인자를 전달한다. 따라서
   Airflow batch image의 `Autoresearch` ref만 최신화하면 merge가 runtime에서
   실패한다.
4. Airflow action-log 인자 builder가 공개 CLI가 받지 않는 `--bucket`,
   `--final-output-base-path`, `--final-quarantine-base-path`,
   `--shard-quarantine-base-path`를 생성한다.
5. Airflow는 `--overwrite <boolean>` 형태를 사용하지만 현재 application CLI는
   값이 없는 `--overwrite` flag만 받는다.
6. `Autoresearch` 공개 job package에는 action-log command만 있고 일일 pipeline의
   첫 단계인 YouTube 수집 command가 아직 없다.
7. Application batch image가 Airflow 저장소에서 `Autoresearch` source를 clone해
   조립되고, Airflow release가 application과 Airflow image를 함께 build한다.
8. 운영 Helm values는 application image를 immutable digest가 아닌 tag로 선택한다.
9. DAG는 git-sync의 `HEAD`로 갱신되지만 `autoresearch_airflow.dag_config`는 Airflow
   image에 포함되어 DAG와 helper가 서로 다른 revision이 될 수 있다.
10. `Autoresearch`에는 레거시 DAG, Astro Dockerfile과 Airflow 설정이 남아 있고,
    `Autoresearch-airflow`에는 domain data quality 구현과 batch image build가 남아
    있다.

각 저장소의 현재 단위 테스트가 통과하는 것만으로는 이 결합을 검증할 수 없다.
Airflow의 repository contract test 일부는 오히려 wrapper 호출과 application source
clone을 현재 정답으로 고정하고 있으므로 전환 단계에서 함께 교체해야 한다.

## 4. 설계 원칙과 불변 조건

### 4.1 공개 process 계약만 교차 저장소 경계로 사용한다

- Airflow source는 `autoresearch.*` 내부 Python API를 import하지 않는다.
- Airflow가 application에서 아는 것은 공개 module command, CLI 인자, 환경 변수,
  exit code, JSON Lines event와 image digest뿐이다.
- Application 내부 함수·module·schema 구현은 public batch contract를 지키는 한
  Airflow 변경 없이 수정할 수 있다.

### 4.2 애플리케이션이 데이터 동작을 소유한다

- GCS input 존재와 schema의 최종 판정은 application이 수행한다.
- path normalization, idempotency, skip/overwrite와 final publish는 application이
  수행한다.
- shard 0을 포함한 모든 shard는 동일한 데이터 역할만 수행한다.
- Airflow는 final artifact를 사전 삭제하거나 application 실패를 보상하기 위해
  output을 삭제하지 않는다.
- CTR, candidate, fingerprint, checkpoint, quarantine과 품질 임계치의 의미는
  application이 소유한다.

### 4.3 Airflow가 실행 정책을 소유한다

- partition date, schedule, fan-out 수, task dependency와 trigger rule은 Airflow가
  정한다.
- Airflow retry, timeout, Pool, pod resource, namespace와 service account는
  Airflow가 정한다.
- 운영 파라미터 값은 Airflow가 선택하지만 application의 허용 범위를 넓히지
  않는다.
- QA의 run-scoped prefix와 `dag_run.conf` allowlist는 Airflow가 더 엄격하게
  제한할 수 있다.

### 4.4 배포 단위와 rollback 단위를 분리한다

- Application release는 checkout된 `Autoresearch` revision 하나만으로 image를
  build한다.
- Airflow release는 application source를 clone하거나 application image를 build하지
  않는다.
- Airflow는 검증된 application image를 digest로 선택한다.
- Application rollback은 이전 digest 선택, DAG rollback은 git-sync revision
  rollback으로 각각 수행한다.

## 5. 범위

### 5.1 이번 전환에 포함한다

- YouTube 일일 수집 공개 CLI 구현
- action-log 공개 CLI의 KPO 호환 boolean 입력과 stdout telemetry 보완
- canonical application batch image build·push 소유권 이전
- Airflow KPO의 공개 CLI 직접 호출 전환
- complete canonical GCS path 전달
- DAG helper의 git-sync revision 동기화
- QA digest 검증과 production 승격 절차
- action-log data quality 구현의 application 이동과 후속 KPO 연결
- 두 저장소의 레거시 DAG, wrapper, 중복 build 표면 제거
- 위 경계를 고정하는 단위·repository contract·image·E2E 테스트

### 5.2 이번 전환에서 제외한다

- action-log 생성 알고리즘, prompt 또는 CTR 정책 변경
- shard 수, OpenRouter 동시성, Pool slot과 resource limit 튜닝
- Feature Store materialization, 모델 학습·평가와 MLflow DAG 추가
- 신규 GCS bucket, GKE cluster 또는 Airflow 설치 방식 변경
- Terraform resource의 실제 apply
- application image를 목적별 여러 image로 분리하는 최적화

## 6. `Autoresearch` 구현 요구사항

### APP-1. 공개 YouTube 일일 수집 command

`autoresearch.jobs.youtube_trending`을 추가하고 공개 실행 계약의 다음 command를
구현한다.

```text
python -m autoresearch.jobs.youtube_trending \
  --partition-date YYYY-MM-DD \
  --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
  --region-code KR \
  --max-results 200 \
  [--proxy-url <url>] \
  [--overwrite[=<boolean>]]
```

Command는 다음을 만족해야 한다.

- API key는 `YOUTUBE_API_KEYS`, `YOUTUBE_API_KEY` 환경 변수에서만 읽는다.
- `ResilientYouTubeClient`, collection과 load 계층을 호출하는 public process
  adapter만 구현하고 수집 로직을 복제하지 않는다.
- 기존 성공 partition이 있고 overwrite가 false면 외부 API를 호출하지 않고
  `status=skipped`, exit 0으로 종료한다.
- 모든 성공·실패 결과는 공개 JSON Lines/exit code 계약을 따른다.
- `--help`, `--version`은 network와 credential 없이 실행된다.

일일 pipeline cutover의 blocker는 `youtube_trending`이다. 수동 backfill command는
동일한 public job pattern으로 구현하되 일일 DAG 전환과 분리된 후속 PR로 진행할 수
있다.

### APP-2. orchestration-friendly overwrite 입력

KPO의 templated argument list가 true/false에 따라 원소 개수를 바꾸지 않아도 되도록
v1에 다음 backward-compatible 입력을 추가한다.

```text
--overwrite
--overwrite=true
--overwrite=false
```

- bare `--overwrite`는 기존과 같이 true다.
- 명시값은 대소문자를 구분하지 않는 `true`, `false`만 허용한다.
- 다른 값은 실행 전 validation 실패와 exit 2로 처리한다.
- 옵션이 없을 때 기본값은 false다.
- YouTube, action-log single/shard/merge에서 같은 의미를 사용한다.
- 공개 실행 계약 문서와 parser 단위 테스트를 같은 PR에서 갱신한다.

Airflow는 한 argument 원소로 `--overwrite=<rendered-boolean>`을 전달한다. 빈 문자열,
`--overwrite false` 뒤의 고아 positional value 또는 shell command 조립을 사용하지
않는다.

### APP-3. action-log public CLI telemetry 소유권

현재 Airflow wrapper에 있는 telemetry stdout 설정과 민감 필드 방어를 application
public process boundary로 이동한다.

- action-log pipeline과 OpenRouter logger의 INFO 이상 JSON event를 stdout에
  prefix 없이 전달한다.
- stdout의 각 non-empty line은 하나의 JSON object여야 한다.
- prompt, request/response body, API key, token, user/persona 식별 필드를 차단한다.
- unrelated library logger의 INFO level을 전역 활성화하지 않는다.
- 마지막 stdout event는 항상 가능한 범위에서 `job_summary`다.
- 설정 함수는 여러 번 호출해도 handler가 중복되지 않는다.

Airflow는 `get_logs=True`로 stdout을 전달할 뿐 telemetry format이나 필터를
구현하지 않는다.

### APP-4. action-log quality command

Airflow 저장소의 `scripts/check_action_log_data_quality.py`가 가진 domain 품질 판정을
application으로 이동하고 다음 공개 command를 제공한다.

```text
python -m autoresearch.jobs.action_log_quality \
  --partition-date YYYY-MM-DD \
  --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
  --virtual-users-path gs://<bucket>/asset/virtual_user/<file>.parquet \
  --action-log-base-path gs://<bucket>/data_lake/action_log \
  --expected-model <model>
```

- YouTube video ID, virtual user ID, event type, model과 final schema를 검증한다.
- domain validation과 threshold는 application test로 고정한다.
- stdout/exit code는 공통 public batch contract를 따른다.
- Airflow quality task는 command와 path만 선택하며 판정 로직을 포함하지 않는다.
- 초기 cutover QA에서는 수동 실행할 수 있지만 레거시 wrapper 삭제 전에는 DAG의
  merge downstream task로 연결한다.

### APP-5. canonical application batch image

`Autoresearch` checkout만으로 application batch image를 build한다.

- build context와 Dockerfile은 `Autoresearch`가 소유한다.
- 다른 저장소를 clone하거나 복사하지 않는다.
- runtime dependency, `autoresearch` package와 공개 job module만 포함한다.
- Airflow, DAG, Helm과 `autoresearch_airflow_jobs`를 포함하지 않는다.
- dependency resolution은 `pyproject.toml`과 `uv.lock`을 단일 원본으로 사용한다.
- image는 non-root user로 실행한다.
- 다음 OCI metadata를 포함한다.

```text
org.opencontainers.image.source=https://github.com/SKYAHO/Autoresearch
org.opencontainers.image.revision=<full-git-sha>
io.autoresearch.batch-contract.version=batch-contract-v1
```

Image CI는 최소 다음을 실행한다.

```text
python -m autoresearch.jobs.youtube_trending --help
python -m autoresearch.jobs.youtube_trending --version
python -m autoresearch.jobs.action_log --help
python -m autoresearch.jobs.action_log --version
python -m autoresearch.jobs.action_log_quality --help
python -m autoresearch.jobs.action_log_quality --version
```

### APP-6. application image release

`Autoresearch` release workflow가 application image를 직접 build·push한다.

- release tag 또는 명시적인 full commit SHA를 checkout한다.
- branch 이름이나 움직이는 `main`을 image source ref로 기록하지 않는다.
- tag를 제공할 수 있지만 workflow 결과에 registry digest와 full source SHA를
  반드시 출력한다.
- build 후 OCI revision과 CLI `--version` revision이 checkout SHA와 같은지
  검증한다.
- 이전 정상 digest를 rollback 후보로 보존한다.
- Airflow 저장소의 상태나 checkout을 요구하지 않는다.

## 7. `Autoresearch-airflow` 구현 요구사항

### AIR-1. KPO의 공개 CLI 직접 호출

일일 DAG의 command를 다음으로 교체한다.

```text
collect: python -m autoresearch.jobs.youtube_trending
shard:   python -m autoresearch.jobs.action_log --mode shard
merge:   python -m autoresearch.jobs.action_log --mode merge
quality: python -m autoresearch.jobs.action_log_quality
```

`autoresearch_airflow_jobs` Python wrapper를 command 경로로 사용하지 않는다.

### AIR-2. canonical action-log shard 인자

각 shard KPO는 공개 계약에 있는 인자만 전달한다.

```text
--mode shard
--partition-date <rendered-date>
--youtube-base-path <complete-gs-path>
--virtual-users-path <complete-gs-path>
--output-base-path <shard-work-gs-path>
[--quarantine-base-path <diagnostic-gs-path>]
--shard-index <index>
--shard-count <count>
--progress-base-path <complete-gs-path>
--checkpoint-base-path <complete-gs-path>
--generator-name <name>
--model-name <name>
--candidates-per-user <count>
--target-ctr <ratio>
--personalized-ratio <ratio>
--popular-ratio <ratio>
--exploration-ratio <ratio>
--seed <int>
--max-concurrency <count>
--chunk-size <count>
--max-quarantine-ratio <ratio>
--overwrite=<boolean>
```

다음 legacy 인자는 전달하지 않는다.

```text
--bucket
--final-output-base-path
--final-quarantine-base-path
```

Shard 0에 final artifact 삭제나 실행 준비 역할을 추가하지 않는다.

### AIR-3. canonical action-log merge 인자

Merge KPO는 다음 인자만 전달한다.

```text
--mode merge
--partition-date <rendered-date>
--shard-count <count>
--shard-output-base-path <shard-work-gs-path>
--output-base-path <final-gs-path>
--max-quarantine-ratio <ratio>
--overwrite=<boolean>
```

다음 legacy 인자는 전달하지 않는다.

```text
--bucket
--quarantine-base-path
--shard-quarantine-base-path
```

Airflow는 merge 전후에 final 또는 quarantine artifact를 삭제하지 않는다.

### AIR-4. complete GCS path 설정

Production Helm values는 빈 path와 `--bucket` fallback 대신 완전한 canonical path를
제공한다.

```text
AIRFLOW_VAR_YOUTUBE_TRENDING_BASE_PATH
AIRFLOW_VAR_ACTION_LOG_YOUTUBE_BASE_PATH
AIRFLOW_VAR_ACTION_LOG_VIRTUAL_USERS_PATH
AIRFLOW_VAR_ACTION_LOG_OUTPUT_DIR
AIRFLOW_VAR_ACTION_LOG_SHARD_WORK_DIR
AIRFLOW_VAR_ACTION_LOG_SHARD_QUARANTINE_DIR
AIRFLOW_VAR_ACTION_LOG_PROGRESS_DIR
AIRFLOW_VAR_ACTION_LOG_CHECKPOINT_DIR
```

- 값은 `gs://bucket/path` 형식이다.
- production 값은 environment-specific Helm values가 소유한다.
- bucket/resource 이름은 infra output에서 전달받을 수 있지만 Airflow source나
  application source에 하드코딩하지 않는다.
- QA override는 기존 all-or-nothing, run-scoped prefix 제한을 유지한다.
- 동일 QA run의 input, work, checkpoint와 final path는 production path와 섞이지
  않는다.

### AIR-5. immutable application image digest

- `AIRFLOW_VAR_AUTORESEARCH_BATCH_IMAGE`는
  `repository/image@sha256:<digest>` 형식을 사용한다.
- release tag만으로 production image를 선택하지 않는다.
- QA는 `AUTORESEARCH_BATCH_IMAGE_OVERRIDE`로 후보 digest를 선택할 수 있다.
- QA 성공 후 environment value의 production digest를 갱신하고 override를
  제거한다.
- image pull policy에 의존해 같은 tag의 새 내용을 받는 운영을 하지 않는다.

### AIR-6. DAG와 helper의 같은 revision 보장

`autoresearch_airflow/dag_config.py`와 DAG가 같은 git-sync revision으로 배포되도록
helper package를 `dags/` subtree 안으로 이동한다.

권장 layout:

```text
dags/
├── autoresearch_airflow/
│   ├── __init__.py
│   └── dag_config.py
├── youtube_gcs_action_log_pipeline_factory.py
├── youtube_gcs_action_log_pipeline.py
└── youtube_gcs_action_log_pipeline_qa.py
```

- helper directory는 `.airflowignore`로 DAG discovery만 제외할 수 있지만 Python
  import는 가능해야 한다.
- helper 변경 때문에 Airflow runtime image를 먼저 배포해야 하는 구조를 제거한다.
- `docker/airflow/Dockerfile`은 Airflow runtime/provider dependency가 필요한 동안
  유지할 수 있지만 DAG helper를 COPY하지 않는다.

### AIR-7. orchestration-only tests

Airflow CI는 다음을 검증한다.

- DAG import와 parse
- collect → shards → merge → quality dependency
- 모든 shard의 동일한 retry, timeout, Pool과 resource policy
- 정확한 public module command와 v1 인자 mapping
- secret이 CLI 인자에 포함되지 않고 `secretKeyRef`로 연결됨
- QA conf allowlist와 path containment
- image가 digest 형식임
- `dags/`와 Airflow runtime source가 `autoresearch.*` 내부 API를 import하지 않음
- Airflow Dockerfile/workflow가 `Autoresearch`를 clone하거나 application batch
  image를 build하지 않음
- Helm lint와 template render

Wrapper 내부 동작을 mock하는 기존 테스트는 공개 KPO 계약 테스트로 교체한다.

## 8. `Autoresearch-infra` 선행조건

이 전환의 주 대상은 application과 Airflow 저장소지만 application-owned release를
위해 다음 최소 인프라 변경이 필요하다.

- GitHub OIDC provider가 `SKYAHO/Autoresearch` repository principal을 허용한다.
- `Autoresearch` workflow가 사용할 GAR writer service account impersonation을
  repository principal로 제한한다.
- 권한은 대상 Artifact Registry repository의 writer에 한정한다.
- Airflow와 application 저장소의 principal binding은 각각 명시한다. 별도 service
  account 사용을 권장하지만, service account를 공유하더라도 repository별
  `principalSet` 제한과 최소 권한을 유지한다.
- Terraform plan·review·apply는 `Autoresearch-infra`의 별도 issue/PR에서 수행한다.

Infra는 application 또는 Airflow image build step을 소유하지 않는다.

## 9. 전환 순서

운영 중인 DAG를 중단하지 않기 위해 `추가 → 검증 → 전환 → 관찰 → 삭제` 순서를
강제한다.

### Phase 0. 기준선 고정

1. 세 저장소를 각각 `origin/main`과 동기화한다.
2. 현재 production DAG ID, image tag/digest, Airflow helper image, Pool과 Helm
   release revision을 기록한다.
3. 현재 final GCS partition과 마지막 성공 run을 rollback 기준으로 기록한다.
4. 기존 pipeline을 변경하지 않은 상태에서 양쪽 CI를 통과시킨다.

**종료 조건**: 변경 전 상태로 돌아갈 수 있는 DAG revision, image와 데이터 기준이
기록되어 있다.

### Phase 1. Application public surface 완성

1. `youtube_trending` command를 추가한다.
2. overwrite explicit boolean 호환 입력을 추가하고 공개 계약을 갱신한다.
3. telemetry stdout/filter를 action-log public CLI로 이동한다.
4. `action_log_quality` command와 test를 추가한다.
5. canonical application image를 build하고 모든 command smoke test를 실행한다.

**종료 조건**: Airflow checkout 없이 application image 하나에서 collect, shard,
merge와 quality command가 실행된다.

### Phase 2. Application release 활성화

1. Infra PR로 `Autoresearch` WIF/GAR push 권한을 추가한다.
2. `Autoresearch` release workflow를 활성화한다.
3. full source SHA를 사용해 후보 image를 build·push한다.
4. digest, OCI label과 CLI `--version` 일치를 검증한다.

**종료 조건**: 후보 application digest가 Airflow 저장소의 build 없이 발행된다.

#### Phase 2 운영 계약과 진행 상태

`Autoresearch`의 `Release application image` workflow는 두 경로만 허용한다.

- GitHub Release `published`: release tag를 checkout하고 release tag와
  `sha-<full-source-sha>` image tag를 함께 push한다.
- 수동 `workflow_dispatch`: 입력한 40자리 `source_sha`만 checkout하고
  `sha-<full-source-sha>` image tag를 push한다. branch와 움직이는 `main`은
  입력으로 허용하지 않는다.

workflow는 다음 repository variable과 secret을 사용한다.

| 종류 | 이름 | 값/출처 |
| --- | --- | --- |
| Variable | `GCP_PROJECT_ID` | 대상 GCP project ID |
| Variable | `GCP_REGION` | Artifact Registry region |
| Variable | `GAR_REPOSITORY` | 대상 Artifact Registry repository 이름 |
| Variable | `WIF_PROVIDER_ID` | bootstrap output의 full provider resource ID |
| Secret | `GAR_PUSHER_SA` | dev output `github_actions_app_pusher_service_account_email` |

build 결과는 GitHub Actions summary와 job output에 full source SHA 및
`autoresearch-batch@sha256:<digest>`를 기록한다. push 직후 digest로 image를 다시
pull하여 OCI revision, non-root user, 세 공개 CLI의 `--help`와 `--version`을
검증한다. 기존 digest와 tag를 삭제하지 않으므로 이전 정상 digest를 rollback에
사용할 수 있다.

image 검증이 끝나면 같은 workflow가 GitHub App installation token으로
`Autoresearch-airflow`의 `deploy/airflow/values.yaml`만 변경하는 승격 PR을 연다.
PR branch는 `automation/batch-<source-sha 앞 12자리>`로 고정해 같은 release의
재실행을 멱등하게 처리한다. 자동화는 PR 생성까지만 담당하고 merge는 사람이
CI 결과와 digest를 확인한 뒤 수행한다. Airflow 저장소의 `main`에 merge되면
그 저장소가 소유한 GKE Helm 배포 workflow가 실행된다.

이를 위해 `APP_ID`와 `APP_PRIVATE_KEY`가 가리키는 GitHub App에는
`Autoresearch-airflow` repository의 Contents read/write 및 Pull requests
read/write 권한이 필요하다. `GITHUB_TOKEN` 대신 installation token을 사용하므로
생성된 PR에서도 대상 저장소 CI가 정상적으로 시작된다.

2026-07-13 기준 진행 상태:

- [x] `Autoresearch-infra`의 repository-scoped WIF impersonation과 GAR writer 적용
- [x] `Autoresearch` 단독 checkout/build/push/검증 workflow 구현 (Issue #134)
- [x] `Autoresearch` repository variable 4개와 `GAR_PUSHER_SA` 설정
- [x] workflow를 `main`에 merge
- [x] full source SHA 후보 image 발행 및 digest 검증
- [x] 발행된 digest를 사용하는 Airflow QA cutover
- [x] 검증된 digest를 Airflow values로 승격하는 자동 PR workflow 구현 (Issue #145)

### Phase 3. Airflow QA cutover

1. DAG helper를 git-sync되는 `dags/` subtree로 이동한다.
2. KPO command와 argument builder를 public CLI에 맞춘다.
3. complete GCS production path와 isolated QA path를 설정한다.
4. QA DAG에 후보 application digest를 override한다.
5. production과 같은 5개 shard topology로 isolated QA를 실행한다.
6. merge 뒤 `action_log_quality` task를 실행한다.

**종료 조건**: QA run이 collect → shards → merge → quality를 모두 통과하고,
Airflow source에 application 내부 import가 없다.

### Phase 4. Production cutover와 관찰

1. production application image 값을 검증된 digest로 갱신한다.
2. 같은 schedule을 가진 레거시 DAG가 활성화돼 있으면 pause한다.
3. 새 DAG revision이 scheduler에 parse됐는지 확인한다.
4. production DAG와 promoted digest를 사용하는 isolated canary를 실행한다.
5. stdout의 progress event와 마지막 `job_summary`, 모든 task exit code를 확인한다.
6. 최종 partition과 quality summary를 확인한다.

**종료 조건**: production DAG의 isolated canary가 새 digest와 public CLI로
collect → shards → merge → quality를 통과하고, 이전 digest와 DAG revision이
rollback 후보로 보존돼 있다.

전체 production virtual-user 입력을 사용하는 scheduled run은 기능 cutover의
필수 gate가 아니다. 현재 6,983 users와 기본 24 candidates 조합은 최대 약
167,592 impressions를 생성하므로, 비용·quota·처리시간·KST 10:00 SLA를 검증하는
별도 capacity 작업으로 관리한다. 단계별 검증은
`SKYAHO/Autoresearch-airflow#44`에서 100 → 1,000 → 승인된 full 순서로 수행한다.

2026-07-13 production canary evidence:

- DAG run: `production_cutover_20260713T114200Z`
- image digest:
  `sha256:6acc380c120f997f6e4aafb15d1c338a531275ba90fbeec889afc5c66c912cc2`
- topology: collect + 5 shards + merge + quality, 8/8 task success
- output: 318 rows, CTR 0.02, schema/reference/quality errors 0
- public command와 단일 `--overwrite=True` argv 및 실제 pod image ID 확인
- full scheduled run은 운영 승인에 따라 보류하고 production DAG를 pause

### Phase 5. 레거시 제거

Production canary 관찰이 끝난 뒤 별도 cleanup PR로만 삭제한다. 전체 규모
capacity/SLA 검증은 cleanup entry gate가 아니다.

단, 대체 기능이 없는 legacy surface는 삭제하지 않는다. 특히
`dags/youtube_backfill_kr.py`는 `Autoresearch#138`의 public backfill CLI와
Airflow KPO DAG가 검증된 뒤에만 제거한다.

`Autoresearch` 제거 대상:

```text
dags/
airflow_settings.yaml
Astro용 root Dockerfile
packages.txt 등 Astro 전용 파일
Airflow DAG 전용 test
Astro/DAG 소유권을 설명하는 stale 문서
```

`Autoresearch-airflow` 제거 대상:

```text
autoresearch_airflow_jobs/
docker/batch/Dockerfile
batch image Cloud Build/GitHub Actions step
wrapper 전용 test
scripts/check_action_log_data_quality.py
application source ref와 clone을 설명하는 문서
```

`scripts/check_action_log_data_quality.py`는 application의 public quality command로
대체된 뒤 제거한다.

2026-07-13 구현 상태:

- [x] `Autoresearch-airflow`의 wrapper, application 내부 import, 중복 batch build와
      application source clone 제거
- [x] public backfill CLI와 Airflow KPO DAG의 isolated smoke 검증
- [x] promoted digest와 새 public CLI를 사용하는 production isolated canary 검증
- [x] `Autoresearch`의 `dags/`, Astro 설정·build 파일과 DAG 전용 test 제거
- [x] 현재 agent guide·application 문서를 최종 소유권으로 갱신

**종료 조건**: 두 저장소에 책임 경계를 역행하는 source, build와 test가 남아 있지
않다.

### Phase 6. 전환 결과 시각화

Phase 1~5와 production 관찰이 모두 끝난 뒤, 이 명세를 단일 원본으로 사용해
AS-IS와 TO-BE를 비교하는 로컬 열람용 HTML을 작성한다.

권장 산출물 경로:

```text
docs/visualizations/autoresearch-airflow-boundary-as-is-to-be.html
```

HTML은 외부 서비스나 CDN 없이 로컬 브라우저에서 열 수 있는 단일 파일로 만들고,
최소한 다음 변화를 나란히 시각화한다.

- 세 저장소의 책임과 소유 파일
- collect → shard → merge → quality 실행 흐름
- application image build·release와 Airflow image 선택 경계
- 직접 Python import·source clone 결합의 제거
- 이동·대체·삭제된 wrapper, DAG, build와 품질 검사 구성요소
- 전환 전후 rollback 단위와 운영 검증 지점

각 시각 요소는 이 명세의 근거 절과 최종 migration PR을 추적할 수 있어야 한다.
HTML은 명세를 대체하는 새 단일 원본이 아니라 완료된 변경을 쉽게 설명하는
파생 산출물이다.

**종료 조건**: 실제 최종 저장소 상태와 일치하는 AS-IS/TO-BE HTML을 로컬에서
열어 책임·실행·배포 경계의 변화를 한 화면에서 확인할 수 있다.

## 10. 검증 전략

### 10.1 `Autoresearch` 단위·통합 검증

- YouTube CLI parser, secret loading, skip/overwrite와 exit code
- action-log mode별 parser와 explicit boolean 호환
- stdout JSON Lines, final summary와 민감정보 차단
- GCS path normalization과 input validation
- shard 동일 역할, checkpoint resume와 merge fingerprint
- merge 실패 시 기존 final 보존
- data quality의 event/user/video/model 참조 검증
- 실제 network를 사용하지 않는 local filesystem fixture 기반 command test

### 10.2 Application image 검증

- image build
- 모든 public command의 `--help`, `--version`
- source revision·contract label 검증
- Airflow package와 wrapper가 image에 없음을 확인
- non-root runtime 확인
- local fixture를 이용한 최소 single 또는 shard/merge smoke test

### 10.3 `Autoresearch-airflow` 검증

- DAG parse/import test
- KPO command·argument·env mapping snapshot 또는 구조 테스트
- fan-out/fan-in/quality dependency
- retry, timeout, Pool, resources와 trigger rule
- QA path allowlist/containment
- internal import, app clone과 app image build 금지 contract test
- Helm lint/template과 digest format

### 10.4 End-to-end QA

최소 QA evidence는 다음을 포함한다.

| 검증 | 기대 결과 |
| --- | --- |
| YouTube collect | QA partition 생성 또는 명시적 skip |
| shard topology | 5개 shard, 동일 fingerprint/user universe |
| checkpoint | shard별 fingerprint namespace part 생성 |
| merge | manifest 검증 후 final parquet 마지막 게시 |
| quality | event type, model, user/video 참조 무결성 통과 |
| logging | prefix 없는 JSON event와 마지막 `job_summary` |
| secret | args/log/rendered values에 secret 원문 없음 |
| rollback | 이전 digest로 image 선택 복구 가능 |

## 11. Rollback

### Application 후보 QA 실패

- production digest는 변경하지 않는다.
- QA override를 제거한다.
- 실패한 digest와 evidence를 보존하고 새 application PR에서 수정한다.

### Production application 실패

- Airflow의 application image 값을 직전 정상 digest로 되돌린다.
- DAG revision은 CLI가 호환되는 경우 유지한다.
- 실패한 rerun이 남긴 work/checkpoint는 별도 namespace로 보존하고 final parquet을
  임의 삭제하지 않는다.

### DAG 실패

- application digest는 유지하고 git-sync revision을 직전 정상 Airflow commit으로
  고정하거나 revert한다.
- 원인이 helper/DAG mismatch라면 image rebuild 대신 같은 git-sync subtree의
  helper와 DAG를 함께 되돌린다.

### Infra 인증 실패

- 기존 release 경로를 삭제하지 않은 상태에서 WIF/GAR binding을 수정한다.
- 실제 Terraform apply 전 plan에서 principal, role과 repository scope를 확인한다.
- 장기적으로 application image를 Airflow 저장소에서 다시 build하는 방식으로
  rollback하지 않는다.

## 12. Issue와 PR 분해

각 항목은 독립 issue/PR로 진행하고, 선행 PR의 merge·release 결과를 다음 PR이
소비한다.

1. **Autoresearch — public command 보완**
   - YouTube CLI, explicit overwrite, telemetry, quality command와 test
2. **Autoresearch — canonical image와 release**
   - Dockerfile/dependency 정리, image CI와 release workflow
3. **Autoresearch-infra — application release IAM**
   - WIF provider allowlist, repo-scoped impersonation과 GAR writer
4. **Autoresearch-airflow — DAG/helper co-versioning**
   - helper를 git-sync subtree로 이동하고 Airflow image coupling 제거
5. **Autoresearch-airflow — public CLI QA cutover**
   - direct command, canonical args/path, candidate digest와 QA evidence
6. **Autoresearch-airflow — production digest 승격**
   - Helm value, isolated production canary와 rollback evidence
7. **Autoresearch-airflow — wrapper/build cleanup**
   - wrapper, batch Dockerfile/workflow, legacy tests와 문서 제거
8. **Autoresearch / Autoresearch-airflow — backfill 이관**
   - public backfill CLI, manual KPO DAG와 smoke evidence
9. **Autoresearch — Astro/DAG cleanup**
   - backfill 이관 뒤 legacy DAG/settings/Dockerfile/test와 stale 문서 제거
10. **Workspace — migration AS-IS/TO-BE 시각화**
   - 모든 cleanup과 production 관찰 뒤 spec 기반 단일 HTML 작성

두 저장소를 동시에 깨뜨리는 cross-repository breaking PR을 만들지 않는다.

## 13. 완료 조건

다음을 모두 만족해야 1차 책임 분리 목표를 완료한 것으로 본다.

- [x] `Autoresearch` image만으로 YouTube collect, action-log shard/merge와 quality
      command를 실행할 수 있다.
- [x] Application image는 `Autoresearch` checkout 하나로 build·release된다.
- [x] Airflow 저장소는 application source를 clone하거나 application image를
      build하지 않는다.
- [x] Airflow DAG와 helper는 같은 git-sync revision으로 배포된다.
- [x] Airflow source는 `autoresearch.*` 내부 Python API를 import하지 않는다.
- [x] 모든 KPO는 공개 module command와 지원되는 v1 인자만 사용한다.
- [x] Production은 application image를 immutable digest로 선택한다.
- [x] Airflow는 GCS final artifact 삭제, schema 판정과 quality 계산을 하지 않는다.
- [x] Application stdout은 JSON Lines와 final `job_summary` 계약을 지킨다.
- [x] 5-shard QA가 collect → shard → merge → quality 전체 경로를 통과한다.
- [x] Production DAG의 isolated canary가 promoted digest와 새 경계로 성공한다.
- [x] 이전 application digest와 Airflow revision으로 각각 독립 rollback할 수 있다.
- [x] `Autoresearch`의 레거시 Airflow 표면과 `Autoresearch-airflow`의 wrapper/domain
      logic/중복 batch build가 제거됐다.
- [x] 두 저장소의 README, agent guide와 운영 runbook이 최종 소유권과 일치한다.
- [ ] 이 명세와 최종 migration PR을 근거로 한 로컬 AS-IS/TO-BE HTML이 실제
      저장소 상태와 일치한다.

## 14. 주요 리스크와 대응

| 리스크 | 대응 |
| --- | --- |
| 최신 application과 legacy merge wrapper의 인자 불일치 | wrapper에 최신 ref만 주입하지 않고 public CLI QA 후 digest 전환 |
| boolean Jinja가 빈 positional argument를 생성 | `--overwrite=<boolean>` v1 호환 입력과 parser test |
| candidate tag가 재사용되어 실행 내용이 변함 | production/QA 모두 registry digest 사용 |
| DAG가 helper image보다 먼저 git-sync됨 | helper를 `dags/` subtree로 이동해 같은 revision 보장 |
| QA path 일부만 override돼 production과 혼합 | all-or-nothing allowlist와 run-scoped prefix 유지 |
| merge 실패 시 이전 정상 final 손상 | app-owned atomic publish, Airflow 사전 삭제 금지 |
| telemetry 이동 후 progress log 유실 | image smoke와 KPO log에서 JSON progress/final summary 검증 |
| WIF 범위를 과도하게 확장 | repository principal과 GAR writer role로 제한, Terraform plan review |
| cleanup이 cutover보다 먼저 merge됨 | production canary 성공과 rollback evidence를 cleanup entry gate로 강제 |
| full run이 기능 QA에 과도한 비용과 시간을 사용 | capacity/SLA 검증을 별도 이슈로 분리하고 단계별 승인 적용 |
| backfill DAG를 대체 없이 삭제 | public backfill CLI와 Airflow KPO smoke를 Astro cleanup 진입 조건으로 강제 |
