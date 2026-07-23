# Public Batch Execution Contract v1

- **상태**: Proposed
- **날짜**: 2026-07-13
- **이슈**: #125, #129
- **관련 ADR**: `docs/adr/0002-repository-responsibility-boundaries.md`

## 목적

이 문서는 `Autoresearch-airflow`가 application 내부 Python API를 import하지 않고
`Autoresearch` batch image를 실행하기 위한 공개 계약을 정의한다. 같은 명령은
로컬, CI와 KubernetesPodOperator에서 동일하게 동작해야 한다.

이 계약은 현재 운영 범위인 YouTube 일일 수집, YouTube backfill, action-log
single/shard/merge, action-log 품질 검사, offline feature build, Feast
materialize, 일일 추천 결과 적재를 다룬다. 학습·평가,
MLflow, FastAPI serving command는 각 기능이 운영화될 때 별도
revision으로 추가한다.

## 계약 버전

초기 계약 버전은 `batch-contract-v1`이다.

- 호환 가능한 인자 추가와 선택 인자 추가는 v1 안에서 허용한다.
- 기존 인자의 제거, 의미 변경, exit code 변경, 필수 인자 추가는 breaking
  change다.
- breaking change는 새 계약 버전, 새 application image와 Airflow 전환 PR을
  순서대로 배포한다.
- application image에는 다음 OCI label을 기록한다.

```text
org.opencontainers.image.source=https://github.com/SKYAHO/Autoresearch
org.opencontainers.image.revision=<git-sha>
io.autoresearch.batch-contract.version=batch-contract-v1
```

## 공개 명령

package layout 전환 전에는 Python module 실행을 canonical interface로 사용한다.

```text
python -m autoresearch.jobs.youtube_trending [options]
python -m autoresearch.jobs.youtube_backfill [options]
python -m autoresearch.jobs.action_log --mode single [options]
python -m autoresearch.jobs.action_log --mode shard [options]
python -m autoresearch.jobs.action_log --mode merge [options]
python -m autoresearch.jobs.action_log_quality [options]
python -m autoresearch.jobs.feature_store_build [options]
python -m autoresearch.jobs.feast_materialize [options]
python -m src.pipeline.daily_recommendations [options]
```

console script alias를 추가할 수 있지만 Airflow는 v1 동안 위 module 경로를
사용한다. 구현 함수 이름과 module 내부 구조는 공개 계약이 아니다.

모든 명령은 다음 공통 옵션을 제공한다.

```text
--help       인자와 사용 예를 출력하고 exit 0
--version    application revision과 batch contract version을 출력하고 exit 0
```

## 공통 실행 계약

### 입력

- 날짜는 ISO `YYYY-MM-DD`만 허용한다.
- GCS path는 `gs://bucket/path`를 canonical 표현으로 사용한다.
- 빈 문자열, `.`·`..`, 중복 separator가 있는 비정규화 path는 거부한다.
- application은 Airflow의 사전 검증 여부와 관계없이 모든 입력을 검증한다.
- secret은 CLI 인자로 받지 않고 정해진 환경 변수에서만 읽는다.
- overwrite를 지원하는 YouTube 일일 수집과 action-log 명령은 옵션 없음, bare flag와
  explicit boolean을 모두 허용한다.

```text
--overwrite
--overwrite=true
--overwrite=false
```

bare flag는 `true`, 옵션 없음은 `false`다. 명시값은 대소문자를 구분하지 않는
`true`와 `false`만 허용하며 그 외 값은 exit 2로 거부한다. Airflow는 argument
원소 수를 고정하기 위해 `--overwrite=<rendered-boolean>` 한 원소를 전달한다.

### 출력

- stdout은 한 줄에 하나의 JSON object인 JSON Lines 형식을 사용한다.
- 정상 종료 전 마지막 event는 `job_summary`다.
- 사람이 읽는 prefix, timestamp prefix와 multi-line payload를 stdout JSON event에
  붙이지 않는다.
- error detail과 traceback은 stderr로 보낼 수 있다.
- prompt, raw request·response, API key, token, persona와 user 식별자는 log에
  포함하지 않는다.

최소 event envelope:

```json
{
  "event": "job_summary",
  "contract_version": "batch-contract-v1",
  "job": "action_log",
  "status": "succeeded",
  "partition_date": "2026-07-13"
}
```

허용 `status`:

- `succeeded`: 새 결과를 정상 생성
- `skipped`: 이미 성공 결과가 있고 overwrite하지 않음
- `failed`: stdout summary를 보장할 수 있는 검증 실패; process exit는 0이 아님

### exit code

| code | 의미 | Airflow 처리 |
| --- | --- | --- |
| `0` | `succeeded` 또는 명시적 `skipped` | task 성공 |
| `1` | runtime, 외부 API, 입력 데이터·schema 검증, 품질 임계치 실패 | task 실패·retry 정책 적용 |
| `2` | CLI 인자의 문법·type·범위·조합 검증 실패 | task 실패, 자동 retry 비권장 |

Python process가 signal 또는 resource limit로 종료될 때의 code는 runtime이
결정하며 Airflow는 0이 아닌 모든 code를 실패로 취급한다.

### secret 환경 변수

| 이름 | 소비 명령 | 비고 |
| --- | --- | --- |
| `YOUTUBE_API_KEYS` | `youtube_trending` | comma-separated key 목록 |
| `YOUTUBE_API_KEY` | `youtube_trending` | 단일 key fallback |
| `YOUTUBE_PROXY_URL` | `youtube_trending` | 선택값, secret 아님 |
| `OPENROUTER_API_KEY` | `action_log` | OpenRouter generator 사용 시 필수 |

Infra는 secret 저장과 workload 접근 권한을 담당하고, Airflow는 Kubernetes
Secret 또는 Secret Manager 연동 reference를 pod 환경 변수에 연결한다.
Application은 환경 변수를 읽고 누락·빈 값을 검증한다.

## BigQuery feature materialize

- `--project`, `--dataset`, `--raw-dataset`은 BigQuery identifier 문법을 만족하는 필수 인자다.
- `--dataset`은 `user_static_feature`, `user_dynamic_feature`, `video_feature` target table을 가리킨다. `--raw-dataset`은 `data_lake_action_log`, `data_lake_youtube_trending_kr`, `asset_virtual_user_vu_1000` source table을 가리키며, 세 source table은 materialization 전에 모두 존재해야 한다.
- 명령은 `user_static_feature`, `user_dynamic_feature`, `video_feature`를 이 순서로 전체 갱신한다.
- 각 테이블은 transaction 내 `DELETE` + `INSERT`로 갱신한다. 한 테이블의 실패는 기존 행을 유지하고 뒤 테이블 실행을 중단하며 exit 1이다.
- raw 결과가 0행이면 transaction을 실패시킨다.
- 성공 `job_summary`에는 project, dataset, raw_dataset, 대상 table 이름, BigQuery job ID와
  `row_counts`를 포함한다. `row_counts`는 `tables`와 동일한 실행 순서의 table
  name을 key로 하고, 각 transaction이 commit된 뒤 target table에서 조회한 최종
  행 수를 JSON integer 값으로 하는 mapping이다. 결과가 정확히 한 행의 integer
  count가 아니면 stdout에는 세부 값을 노출하지 않고 `runtime_failure`, exit 1로
  실패한다.

## Feast materialize

```text
python -m autoresearch.jobs.feast_materialize \
  [--repo-path feature_repo] \
  [--views VIEW1,VIEW2] \
  [--start-ts ISO8601 --end-ts ISO8601] \
  [--dry-run[=<boolean>]]
```

### 계약

- v1 호환 추가 명령이다 (기존 명령의 계약 변경 없음).
- `--start-ts`/`--end-ts`는 함께만 지정할 수 있다. 하나만 지정하면 exit 2다.
- 둘 다 지정하면 해당 구간 materialize, 둘 다 없으면 현재 UTC 기준
  incremental materialize를 수행한다.
- `--views` 생략 시 registry의 전체 FeatureView가 대상이다. 등록되지 않은
  view 이름은 exit 1이다.
- `--dry-run`은 CA 조달, IAM token 발급, Redis `PING`, registry 접근까지만
  검증하고 적재 없이 exit 0으로 종료한다.
- IAM token, CA 본문, entity 값은 stdout·stderr에 출력하지 않는다.
- 실행 이미지는 `Dockerfile.feast` 파생 이미지다 (`Dockerfile.app`에는 feast
  의존성이 없다).

### 환경 변수

| 이름 | 용도 | 비고 |
| --- | --- | --- |
| `GCP_PROJECT_ID`, `BQ_DATASET`, `GCS_REGISTRY_PATH`, `GCS_STAGING_LOCATION` | Feast offline store·registry | 기존 Feast 설정 |
| `REDIS_HOST`, `REDIS_PORT` | Redis Cluster discovery endpoint | |
| `REDIS_TLS_CA_PATH` | 서버 CA 번들 파일 경로 | 선택 |
| `REDIS_CA_SECRET_ID` | CA 번들 Secret Manager secret id | `REDIS_TLS_CA_PATH` 부재 시 필수 |

## YouTube 일일 수집

```text
python -m autoresearch.jobs.youtube_trending \
  --partition-date YYYY-MM-DD \
  --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
  --region-code KR \
  --max-results 200 \
  [--proxy-url <url>] \
  [--overwrite[=<boolean>]]
```

### 계약

- `--partition-date`, `--youtube-base-path`는 필수다.
- `--max-results`는 1 이상의 정수다.
- API key는 환경 변수에서만 읽는다.
- 성공 파일은 다음 위치에 쓴다.

```text
<youtube-base-path>/dt=YYYY-MM-DD/part-0.parquet
```

- 기존 성공 파일이 있고 `--overwrite`가 없으면 API를 호출하지 않고
  `status=skipped`, exit 0으로 종료한다.
- output parquet는 `autoresearch.youtube_collection.schema`와 load 계층이
  검증한다.

## YouTube backfill

```text
python -m autoresearch.jobs.youtube_backfill \
  --source-path gs://<bucket>/<source>.parquet \
  --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
  --overwrite=true
```

### 계약

- source와 output은 정규화된 완전한 `gs://` path만 허용한다. 로컬 경로 지원은
  application 내부 함수의 개발·테스트 호환 범위이며 공개 운영 계약이 아니다.
- source parquet 전체를 날짜별 partition으로 변환한다. 날짜 범위 필터는 기존
  backfill 비즈니스 로직에 없으므로 v1 공개 명령에서도 제공하지 않는다.
- output은 일일 수집과 동일한 schema·partition path를 사용한다.
- 기존 구현과 동일하게 source에 포함된 각 날짜의 `part-0.parquet`을 교체하므로
  실행자는 `--overwrite=true`를 명시해야 한다. 옵션 누락 또는 false는 실행 전에
  exit 2로 거부한다.
- 같은 source를 다시 실행하면 같은 날짜 partition을 교체하는 멱등 동작이다.
  source에서 사라진 날짜의 기존 destination partition은 자동 삭제하지 않는다.
- 개별 partition 실패는 전체 command를 실패시킨다. 이미 완료된 partition은
  남을 수 있으므로 운영자는 같은 source로 전체 명령을 다시 실행한다.

## Action log 공통 옵션

`single`과 `shard`는 다음 공통 입력을 사용한다.

```text
--partition-date YYYY-MM-DD
--youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr
--virtual-users-path gs://<bucket>/asset/virtual_user/<file>.parquet
--output-base-path gs://<bucket>/<path>
[--quarantine-base-path gs://<bucket>/<path>]
[--max-users <positive-int>]
[--overwrite[=<boolean>]]
[--generator-name <name>]
[--model-name <name>]
[--candidates-per-user <positive-int>]
[--target-ctr <0..1>]
[--personalized-ratio <0..1>]
[--popular-ratio <0..1>]
[--exploration-ratio <0..1>]
[--seed <int>]
[--max-concurrency <positive-int>]
[--chunk-size <non-negative-int>]
[--max-quarantine-ratio <0..1>]
```

### 공통 validation

- YouTube `dt` partition과 virtual user parquet이 존재해야 한다.
- personalized, popular, exploration ratio의 합은 절대 허용 오차 `1e-9` 안에서
  `1.0`이어야 한다. 구현은 `math.isclose`에 `rel_tol=0.0`, `abs_tol=1e-9`를
  적용하거나 동등한 검사를 사용한다.
- 각 ratio가 숫자로 parsing됐지만 합계 조건을 만족하지 않으면 실행 전 CLI 인자
  조합 오류로 처리하고 exit 2로 종료한다.
- `target-ctr`와 `max-quarantine-ratio`는 0 이상 1 이하다.
- `max-users`는 전체 실행의 사용자 universe 제한이다. shard당 제한이 아니다.
- `chunk-size=0`은 `candidates-per-user`와 같은 크기를 사용한다는 뜻이다.
- secret, prompt와 user/persona 식별자는 CLI나 summary에 출력하지 않는다.
- Airflow production DAG는 결과에 영향을 주는 선택 인자를 명시적으로 전달한다.
  Application 기본값은 로컬 실행과 하위 호환을 위한 값이지 운영 설정의 단일
  원본이 아니다.

## Action log single

```text
python -m autoresearch.jobs.action_log --mode single <common-options>
```

성공 파일:

```text
<output-base-path>/dt=YYYY-MM-DD/part-0.parquet
```

기존 final parquet이 있고 `--overwrite`가 없으면 `status=skipped`, exit 0이다.
격리 비율이 임계치 이내이고 생성·schema 검증·final parquet 게시가 완료되면
`succeeded`로 간주한다.

전환 기간의 기존 `youtube_action_log_daily` DAG는 Python 함수의 종전 재생성
동작을 유지한다. 공개 CLI는 함수 기본값에 의존하지 않고 `--overwrite` 여부를
명시적으로 전달하므로, 새 Airflow DAG에서는 플래그가 없을 때 위 skip 계약을
따른다.

`--quarantine-base-path`는 실패 상세를 보존해야 하는 QA·진단 실행에서만
사용하는 선택 인자다. 지정되면 다음 JSONL을 best-effort로 기록한다.

```text
<quarantine-base-path>/dt=YYYY-MM-DD/quarantine.jsonl
```

상세 격리 파일은 정상 데이터 생성에 필요한 입력이 아니다. 저장 실패는
`quarantine_publish_failed` warning event로 기록하되 final parquet을 삭제하거나
task를 실패시키지 않는다. 격리 비율 판정에 필요한 count와 실패 유형 집계는
항상 job summary에 남긴다.

### 노출 소스 (single·shard 공통)

`single`과 `shard` 모드는 노출(candidate) 조립 소스를 선택하는 두 인자를
받는다. `merge` 모드는 두 인자를 모두 거부한다(exit 2, `invalid_arguments`).

| 인자 | 값 | 기본값 | 설명 |
| --- | --- | --- | --- |
| `--exposure-source` | `model` \| `heuristic` \| `rerank-api` | `model` | 노출 조립 소스 선택 |
| `--recommendations-table` | bare table name | env 또는 `user_recommendations` | `model` 모드에서만 유효한 대상 테이블 이름 |
| `--rerank-url` | Inference Server base URL | (없음) | `rerank-api` 모드에서 필수, 그 외 거부 |
| `--rerank-timeout-sec` | 양의 유한 실수 | `30.0` | `rerank-api` 모드에서만 유효한 요청 timeout |

- `--exposure-source model`(기본)은 champion 모델의 유저별 순위를
  `user_recommendations`에서 읽어 70/20/10(모델/트렌딩/랜덤)으로 노출을
  조립하고 노출별 정책 태그를 로그에 싣는다. 이 모드는 BigQuery에 의존한다.
- 대상 테이블 id는 `{CTR_TRAINING_BQ_PROJECT}.{CTR_TRAINING_BQ_DATASET}.<name>`으로
  정규화한다. `<name>`은 `--recommendations-table` → 환경 변수
  `CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE` → `user_recommendations` 순으로 해석한다.
- `model` 모드는 action log와 `user_recommendations`의 dt 파티션이 정합해야 한다.
  action log의 `--partition-date`와 동일한 dt 파티션을 조회한다.
- 해당 dt 파티션이 비어 있으면 fail-fast로 exit 1(`runtime_failure`)이다. 휴리스틱
  대체는 자동으로 일어나지 않는다.
- `--recommendations-table`은 `model` 모드에서만 허용한다. `heuristic` 모드와 함께
  주면 exit 2(`invalid_arguments`)로 거부한다.
- `--exposure-source heuristic`은 종전 규칙 기반 조립으로 폴백한다. 이 모드는
  src 파이프라인·BigQuery에 의존하지 않으므로, `user_recommendations` 파티션이
  아직 없거나 상류 추천 배치가 실패한 날의 복구 실행에 사용한다.
- `--exposure-source rerank-api`(#277)는 유저별로 Inference Server
  `POST /rerank`를 실시간 호출해 순위를 받고, 같은 70/20/10 규칙·정책 태그로
  조립한다(`policy_version`에는 응답 `model_id`가 실린다). **single 모드
  전용**이다 — shard는 체크포인트 재실행 시 다른 점수가 나와 재현성이 깨지므로
  거부한다(exit 2). 호출 실패(재시도 소진·4xx)와 라운드 도중 `model_id` 변경은
  fail-fast로 exit 1이며, 휴리스틱 대체는 일어나지 않는다. 상세는
  `docs/specs/2026-07-23-rerank-api-exposure-source.md` 참조.

#### Airflow 선행 의존 인계 노트

`model` 모드의 action log task는 같은 dt의 `daily_recommendations` 배치(#216,
`user_recommendations` 적재)가 선행 완료되어야 한다. `Autoresearch-airflow`의 DAG는
action log task를 `daily_recommendations` 뒤에 배치하고, 파티션 부재 시 exit 1을
task 실패로 전파한다. 상류 추천이 소실·지연된 날의 수동 복구는
`--exposure-source heuristic`으로 실행한다.

## Action log shard

```text
python -m autoresearch.jobs.action_log --mode shard \
  <common-options> \
  --shard-index <0-based-index> \
  --shard-count <positive-int> \
  --progress-base-path gs://<bucket>/data_lake/action_log_progress \
  --checkpoint-base-path gs://<bucket>/data_lake/action_log_checkpoints
```

### 계약

- `0 <= shard-index < shard-count`를 만족해야 한다.
- shard 0을 포함한 모든 shard는 자신의 사용자 구간에 대한 draft, manifest,
  checkpoint와 progress만 생성한다. 특정 shard에 final artifact 삭제나 실행 준비
  같은 별도 운영 역할을 부여하지 않는다.
- 모든 shard는 `max-users`가 적용된 동일한 사용자 snapshot과 동일한
  `input_fingerprint`를 사용한다.
- 순서는 `max-users cap → fingerprint → shard selection`으로 고정한다.
- shard output과 manifest:

```text
<output-base-path>/dt=YYYY-MM-DD/shard=NNN/part-0.parquet
<output-base-path>/dt=YYYY-MM-DD/shard=NNN/manifest.json
```

- progress는 관측용이며 resume 판단에 사용하지 않는다.

```text
<progress-base-path>/dt=YYYY-MM-DD/shard=NNN/progress.json
```

- checkpoint는 fingerprint namespace 아래의 immutable parquet part만 재사용한다.

```text
<checkpoint-base-path>/dt=YYYY-MM-DD/shard=NNN/
  fingerprint=<sha256>/parts/*.parquet
```

- 기존 final parquet은 shard 실행 전에 삭제하지 않는다. 새 merge가 실패하면
  이전 parquet을 마지막 정상 결과로 유지한다.
- shard별 상세 격리 파일은 `--quarantine-base-path`를 지정한 진단 실행에서만
  best-effort로 저장한다. 저장 실패와 관계없이 manifest에는 `quarantine_count`와
  실패 유형 집계를 기록하며 merge의 전역 임계치 판정은 이 집계를 사용한다.

## Action log merge

```text
python -m autoresearch.jobs.action_log --mode merge \
  --partition-date YYYY-MM-DD \
  --shard-count <positive-int> \
  --shard-output-base-path gs://<bucket>/data_lake/action_log_work \
  --output-base-path gs://<bucket>/data_lake/action_log \
  --max-quarantine-ratio <0..1> \
  [--overwrite[=<boolean>]]
```

### 계약

- `0..shard-count-1`의 모든 manifest와 shard output이 존재해야 한다.
- manifest의 schema, prompt, input와 config fingerprint가 호환되어야 한다.
- global CTR normalization, event ID 확정과 최종 schema 검증은 merge command가
  수행한다.
- 전역 quarantine ratio는 manifest의 `total_work`와 `quarantine_count` 합계로
  계산한다. 상세 격리 JSONL 존재 여부에 의존하지 않는다.
- 전환 기간의 구형 manifest처럼 실패 유형 집계가 없는 경우 해당 격리 건수는
  `unclassified_quarantine_count`로 별도 집계하고
  `quarantine_error_counts_unavailable` warning을 출력한다. 따라서 알려진 세 실패
  유형의 합계가 전체 격리 건수보다 작아지는 이유를 소비자가 구분할 수 있다.
- quarantine ratio가 임계치를 넘으면 final parquet을 게시하기 전에 exit 1로
  실패한다. 기존 final parquet이 있다면 삭제하지 않고 마지막 정상 결과로
  유지한다.
- merge는 shard별 상세 격리 JSONL을 입력으로 요구하거나 하나의 최종 격리 파일로
  다시 합치지 않는다. QA·진단에 필요한 상세는 각 shard의 선택 파일에서 확인한다.
- 모든 draft·manifest·schema 검증이 끝난 뒤 final parquet을 canonical path에 가장
  마지막으로 게시한다. `--overwrite` rerun은 이 시점에만 이전 final parquet을
  교체한다.
- 기존 final parquet이 있고 `--overwrite`가 없으면 이를 유지하고
  `status=skipped`, exit 0으로 종료한다.
- 현재 run의 성공 여부는 final 파일의 단순 존재가 아니라 process exit code와
  마지막 `job_summary`로 판정한다. 실패한 rerun 동안에도 이전 정상 파일이 남을
  수 있기 때문이다.
- 최종 output:

```text
<output-base-path>/dt=YYYY-MM-DD/part-0.parquet
```

## Action log 품질 검사

```text
python -m autoresearch.jobs.action_log_quality \
  --partition-date YYYY-MM-DD \
  --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
  --virtual-users-path gs://<bucket>/asset/virtual_user/<file>.parquet \
  --action-log-base-path gs://<bucket>/data_lake/action_log \
  --expected-model <model>
```

### 계약

- YouTube와 action-log 입력은 지정 날짜의
  `dt=YYYY-MM-DD/part-0.parquet`를 읽는다.
- YouTube `video_id`의 null·중복, 필수 event type, action-log의 video/user 참조,
  기대 model 존재 여부를 검증한다.
- action-log Arrow final schema와 각 row의 `EventLog` 불변식을 함께 검증한다.
- 식별자 원문은 출력하지 않고 row·오류 count와 안전한 판정 문구만
  `job_summary`에 포함한다.
- 모든 검사를 통과하면 `status=succeeded`, exit 0이다. 품질 판정 실패는
  `status=failed`, exit 1이며 CLI 인자 오류는 exit 2다.

## 일일 추천 결과 적재

```text
python -m src.pipeline.daily_recommendations \
  [--candidate-dt YYYY-MM-DD] \
  [--events-dt YYYY-MM-DD] \
  [--max-users <positive-int>] \
  [--output-table <table>] \
  [--max-skip-ratio <0..1>] \
  [--dry-run]
```

### 계약

- v1 호환 추가 명령이다 (기존 명령의 계약 변경 없음).
- champion 모델(`models:/ctr-model@champion`)로 일일 트렌딩 후보를 가상 유저
  전원에 대해 채점해 `user_recommendations` dt 파티션에 멱등 적재한다
  (파티션 데코레이터 + WRITE_TRUNCATE).
- `--candidate-dt` 기본값은 후보 테이블 MAX(dt), `--events-dt` 기본값은 action
  log MAX(dt)이며 **단일 파티션만 소비**한다. `--output-table` 기본값은
  `user_recommendations`, `--max-skip-ratio` 기본값은 0.1이다.
- 환경변수: `MLFLOW_TRACKING_URI`(필수), `RERANK_REGISTRY_MODEL_NAME`(기본
  `ctr-model`), `RERANK_REGISTRY_ALIAS`(기본 `champion`), `CTR_TRAINING_BQ_*`
  (기존 체계), `CTR_TRAINING_BQ_VIRTUAL_USERS_TABLE`(기본
  `asset_virtual_user_vu_1000`), `CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE`(기본
  `user_recommendations`).
- 정상 종료 시 마지막 stdout event는 `event=job_summary`,
  `job=daily_recommendations`, `status=succeeded`이며 `users`,
  `skipped_users`, `rows`, `model_run_id`, `model_version`, `events_dt`,
  `dry_run`을 포함한다. 인자 오류는 exit 2, registry/BQ/후보 0건/skip 임계
  초과는 exit 1이며 실패 summary를 남긴다.
- 격리 진단: user quarantine 요구를 위해 stderr warning에 `user_id`와 예외
  타입만 기록한다. stdout summary에는 user 식별자를 넣지 않으며 persona·원문
  예외 메시지는 기록하지 않는다. 공통 식별자 로그 금지 규칙의 범위가 stdout
  telemetry임을 이 명령 섹션에서 명시한다.
- 스케줄·재시도·타임아웃은 `Autoresearch-airflow` 소유.

## Airflow 호출 계약

Airflow는 다음만 담당한다.

- partition date template과 운영 파라미터 값 결정
- shard 수만큼 KPO task fan-out
- shard 0을 포함한 모든 shard에 동일한 task 정책 적용
- 모든 shard 성공 뒤 merge task fan-in
- image digest, namespace, service account와 secret reference 연결
- task retry, timeout, Pool과 concurrency
- QA `dag_run.conf` allowlist와 run-scoped prefix 정책

Airflow는 다음을 하지 않는다.

- `autoresearch.*` 내부 Python 함수 import
- GCS input existence 또는 schema의 최종 판정
- final artifact 사전 삭제와 publish rollback
- CTR·candidate·fingerprint·quarantine 계산
- application image source 조립

## 기존 wrapper 호환 기간

현재 `Autoresearch-airflow` wrapper는 `--bucket`과 빈 path를 받아 application
외부에서 기본 GCS path를 합성한다. 이 동작은 v1 canonical interface가 아니다.

- 새 공개 CLI와 production DAG는 완전한 `gs://` path를 전달한다.
- 첫 application image는 전환을 위해 기존 `--bucket` 입력을 deprecated alias로
  받을 수 있다.
- 기존 wrapper의 shard 0 final artifact 삭제는 legacy 호환 동작으로만 유지하고,
  새 공개 CLI로 전환할 때 제거한다.
- 기존 wrapper가 merge 단계에서 final quarantine JSONL을 만드는 동작도 legacy
  호환 범위로 두고 새 공개 CLI 전환 시 제거한다.
- deprecated 입력을 사용하면 secret을 포함하지 않는 warning event를 남긴다.
- 새 Airflow DAG의 QA가 통과한 다음 release에서 legacy wrapper와 `--bucket`
  path 합성을 제거한다.
- 호환 기간에도 path normalization과 최종 데이터 검증은 application이 수행한다.

KPO image는 tag가 아닌 immutable digest로 고정한다.

```text
asia-northeast3-docker.pkg.dev/<project>/<repository>/autoresearch-batch@sha256:<digest>
```

## Image release 계약

- Image build context와 Dockerfile은 `Autoresearch`에 있다.
- Build는 checkout된 한 revision만 사용하며 다른 저장소를 clone하지 않는다.
- image에는 runtime dependency, `autoresearch` package와 공개 job module을 넣는다.
- Airflow package, DAG와 Helm file은 넣지 않는다.
- release 결과는 image digest와 OCI label을 제공한다.
- Airflow release는 검증된 digest를 values 또는 Airflow Variable에 고정한다.

권장 배포 순서:

1. 새 CLI를 포함한 application image를 build·push한다.
2. image에서 `--version`, 각 command `--help`와 smoke test를 실행한다.
3. Airflow QA DAG가 새 digest를 사용하게 한다.
4. QA가 통과하면 production image digest를 갱신한다.
5. 최소 한 번의 scheduled run을 관찰한 뒤 이전 image를 rollback 후보로 보존한다.

## Rollback 계약

- Application rollback은 Airflow가 이전 image digest를 다시 선택하는 것으로
  수행한다.
- 실패한 rerun은 기존 final parquet을 마지막 정상 결과로 유지하며 rollback을
  위해 정상 파일을 다시 복사할 필요가 없다.
- DAG rollback은 git-sync 대상 Airflow commit을 되돌리는 것으로 수행한다.
- Infra rollback은 Terraform state와 plan 단위로 수행하며 app/DAG source를
  변경하지 않는다.
- CLI breaking change는 이전 Airflow DAG가 실행할 수 있는 image를 제거하기
  전에 호환 기간을 둔다.
- 동일 schedule의 이전·신규 DAG가 동시에 활성화되지 않도록 cutover 전에 이전
  DAG를 pause한다.

## 검증 계약

### `Autoresearch`

- 모든 CLI parser와 validation 단위 테스트
- action-log single/shard/merge 테스트
- ratio 합계 허용 오차와 CLI 조합 오류 exit 2 테스트
- 모든 shard가 동일한 역할만 수행하는 repository contract test
- checkpoint resume와 merge publish 실패 테스트
- 상세 격리 파일 저장 실패가 final 성공을 막지 않는 테스트
- merge 실패 시 이전 final parquet이 보존되는 테스트
- data quality test
- batch image build, `--version`, `--help`, import smoke test
- secret과 식별자 log 차단 테스트

### `Autoresearch-airflow`

- DAG parse와 import error test
- 정확한 command·argument·environment mapping test
- schedule, dependency, Pool, retry, timeout test
- QA override allowlist와 namespace containment test
- Helm lint와 template render
- DAG가 application 내부 module을 import하지 않는 repository contract test

### End-to-end QA

- production과 같은 KPO 경로에서 1,000명, 5개 shard 실행
- 5개 shard가 동일한 fingerprint와 사용자 universe를 사용
- shard·manifest·checkpoint·final partition 생성
- manifest 집계 기준 quarantine ratio가 허용 임계치 이하
- 선택적 상세 격리 파일 저장 실패가 final partition을 제거하지 않음
- event schema와 user/video 참조 무결성 통과
- pod stdout JSON event와 final `job_summary` 확인
- 이전 image digest로 rollback 가능 확인

## 완료 조건

이 계약의 v1 구현은 다음을 모두 만족할 때 완료된다.

- `Autoresearch` image만으로 공개 명령이 실행된다.
- Airflow repository checkout 없이 application image를 build할 수 있다.
- Airflow DAG는 공개 명령 문자열 외에 application 내부 구현에 의존하지 않는다.
- Airflow와 application release를 각각 독립적으로 rollback할 수 있다.
- 중복 DAG, wrapper domain logic과 batch image build가 제거된다.
- shard 0에 별도 final 삭제 역할이 없고 모든 shard가 동일한 계약을 따른다.
- 상세 격리 파일 저장 여부와 final parquet 성공 여부가 분리된다.
- 다른 저장소 문서는 이 계약을 복사하지 않고 링크한다.
