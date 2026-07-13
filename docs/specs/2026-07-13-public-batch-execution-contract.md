# Public Batch Execution Contract v1

- **상태**: Proposed
- **날짜**: 2026-07-13
- **이슈**: #125
- **관련 ADR**: `docs/adr/0002-repository-responsibility-boundaries.md`

## 목적

이 문서는 `Autoresearch-airflow`가 application 내부 Python API를 import하지 않고
`Autoresearch` batch image를 실행하기 위한 공개 계약을 정의한다. 같은 명령은
로컬, CI와 KubernetesPodOperator에서 동일하게 동작해야 한다.

이 계약은 현재 운영 범위인 YouTube 일일 수집, YouTube backfill, action-log
single/shard/merge를 다룬다. Feature Store materialization, 학습·평가, MLflow,
FastAPI serving command는 각 기능이 운영화될 때 별도 revision으로 추가한다.

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
| `1` | runtime, 외부 API, 데이터 검증, 품질 임계치 실패 | task 실패·retry 정책 적용 |
| `2` | CLI 사용 오류 또는 인자 parsing 실패 | task 실패, 자동 retry 비권장 |

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

## YouTube 일일 수집

```text
python -m autoresearch.jobs.youtube_trending \
  --partition-date YYYY-MM-DD \
  --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
  --region-code KR \
  --max-results 200 \
  [--proxy-url <url>] \
  [--overwrite]
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
  --source-path <local-or-gs-parquet> \
  --youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr \
  [--start-date YYYY-MM-DD] \
  [--end-date YYYY-MM-DD] \
  [--overwrite]
```

### 계약

- source parquet을 날짜별 partition으로 변환한다.
- `start-date`와 `end-date`를 함께 사용할 때 시작일은 종료일보다 늦을 수 없다.
- output은 일일 수집과 동일한 schema·partition path를 사용한다.
- 개별 partition 실패는 전체 command를 실패시킨다. 부분 성공 partition 목록은
  summary에 기록한다.

## Action log 공통 옵션

`single`과 `shard`는 다음 공통 입력을 사용한다.

```text
--partition-date YYYY-MM-DD
--youtube-base-path gs://<bucket>/data_lake/youtube_trending_kr
--virtual-users-path gs://<bucket>/asset/virtual_user/<file>.parquet
--output-base-path gs://<bucket>/<path>
--quarantine-base-path gs://<bucket>/<path>
[--max-users <positive-int>]
[--overwrite]
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
- personalized, popular, exploration ratio의 합은 `1.0`이어야 한다.
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
<quarantine-base-path>/dt=YYYY-MM-DD/quarantine.jsonl
```

기존 final parquet이 있고 `--overwrite`가 없으면 `status=skipped`, exit 0이다.
생성·schema 검증·quarantine publish까지 완료되어야 `succeeded`로 간주한다.

## Action log shard

```text
python -m autoresearch.jobs.action_log --mode shard \
  <common-options> \
  --shard-index <0-based-index> \
  --shard-count <positive-int> \
  --progress-base-path gs://<bucket>/data_lake/action_log_progress \
  --checkpoint-base-path gs://<bucket>/data_lake/action_log_checkpoints \
  --final-output-base-path gs://<bucket>/data_lake/action_log \
  --final-quarantine-base-path gs://<bucket>/data_lake/action_log_quarantine
```

### 계약

- `0 <= shard-index < shard-count`를 만족해야 한다.
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

- stale final artifact 무효화와 publish 성공 판정은 application 책임이다.
  Airflow task가 GCS file을 직접 삭제해 성공 의미를 재구현하지 않는다.
- v1 호환 동작에서는 `shard-index=0` application process가 shard 처리 전에 stale
  final parquet과 quarantine artifact를 무효화한다. 이 동작을 별도 prepare
  command로 분리하려면 새 계약 revision과 DAG dependency 변경이 필요하다.

## Action log merge

```text
python -m autoresearch.jobs.action_log --mode merge \
  --partition-date YYYY-MM-DD \
  --shard-count <positive-int> \
  --shard-output-base-path gs://<bucket>/data_lake/action_log_work \
  --shard-quarantine-base-path gs://<bucket>/data_lake/action_log_quarantine_work \
  --output-base-path gs://<bucket>/data_lake/action_log \
  --quarantine-base-path gs://<bucket>/data_lake/action_log_quarantine \
  --max-quarantine-ratio <0..1>
```

### 계약

- `0..shard-count-1`의 모든 manifest와 shard output이 존재해야 한다.
- manifest의 schema, prompt, input와 config fingerprint가 호환되어야 한다.
- global CTR normalization, event ID 확정과 최종 schema 검증은 merge command가
  수행한다.
- quarantine ratio가 임계치를 넘으면 exit 1이며 성공 parquet을 남기지 않는다.
- final parquet 이후 quarantine publish가 실패해도 final parquet을 성공 marker로
  남기지 않는다.
- 최종 output:

```text
<output-base-path>/dt=YYYY-MM-DD/part-0.parquet
<quarantine-base-path>/dt=YYYY-MM-DD/quarantine.jsonl
```

## Airflow 호출 계약

Airflow는 다음만 담당한다.

- partition date template과 운영 파라미터 값 결정
- shard 수만큼 KPO task fan-out
- 모든 shard 성공 뒤 merge task fan-in
- image digest, namespace, service account와 secret reference 연결
- task retry, timeout, Pool과 concurrency
- QA `dag_run.conf` allowlist와 run-scoped prefix 정책

Airflow는 다음을 하지 않는다.

- `autoresearch.*` 내부 Python 함수 import
- GCS input existence 또는 schema의 최종 판정
- final artifact 삭제와 publish rollback
- CTR·candidate·fingerprint·quarantine 계산
- application image source 조립

## 기존 wrapper 호환 기간

현재 `Autoresearch-airflow` wrapper는 `--bucket`과 빈 path를 받아 application
외부에서 기본 GCS path를 합성한다. 이 동작은 v1 canonical interface가 아니다.

- 새 공개 CLI와 production DAG는 완전한 `gs://` path를 전달한다.
- 첫 application image는 전환을 위해 기존 `--bucket` 입력을 deprecated alias로
  받을 수 있다.
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
- checkpoint resume와 merge publish 실패 테스트
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
- quarantine 0 또는 허용 임계치 이하
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
- 다른 저장소 문서는 이 계약을 복사하지 않고 링크한다.
