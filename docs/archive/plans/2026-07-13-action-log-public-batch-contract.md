# 액션 로그 공개 배치 실행 계약 구현 계획

- **상태**: 구현 및 로컬 검증 완료
- **날짜**: 2026-07-13
- **구현 이슈**: #127
- **선행 이슈**: #125
- **선행 PR**: #126 (merged)
- **기준 계약**: `docs/specs/2026-07-13-public-batch-execution-contract.md`
- **책임 경계**: `docs/adr/0002-repository-responsibility-boundaries.md`

## 목표

`Autoresearch`가 액션 로그 `single`, `shard`, `merge` 작업의 공개 실행
인터페이스와 데이터 게시 정책을 소유하도록 구현한다. 같은 application image를
로컬·CI·KubernetesPodOperator에서 실행할 수 있게 하고, Airflow가
`autoresearch.action_logs` 내부 함수에 의존하지 않게 할 기반을 만든다.

ㅇ
1. `python -m autoresearch.jobs.action_log`가 계약된 인자와 종료 코드를 제공한다.
2. 후보 구성 비율의 합을 절대 허용 오차 `1e-9`로 검증한다.
3. shard 0을 포함한 모든 shard가 동일한 데이터 작업만 수행한다.
4. 선택적 격리 상세 파일 저장 실패가 정상 결과 생성을 실패시키지 않는다.
5. 새 결과의 생성·스키마·품질 검증이 끝난 뒤 최종 Parquet을 마지막으로
   게시하고, 그 전의 실패에는 기존 정상 파일을 보존한다.

## 현재 상태

- `autoresearch/action_logs/daily.py`의 `run_daily_action_log()`,
  `run_daily_action_log_shard()`, `merge_daily_action_log_shards()`가 핵심 실행
  로직을 이미 제공한다.
- 공개 `autoresearch.jobs` package와 action-log module CLI는 아직 없다.
- `EventGenerationRequest`는 각 비율의 `0..1` 범위만 검증하고 세 후보 구성
  비율의 합은 검증하지 않는다.
- single은 final Parquet을 쓴 다음 격리 JSONL을 복사한다. 이 때문에 격리 파일
  복사 실패가 이미 게시된 final을 실패한 실행의 결과로 남길 수 있다.
- shard는 draft를 쓴 뒤 선택적 격리 JSONL 복사가 실패하면 manifest를 쓰지
  못하고 실패한다.
- merge는 shard 격리 JSONL을 다시 읽어 최종 격리 파일로 합치며, final
  Parquet을 먼저 복사한 뒤 격리 파일을 쓴다.
- `Dockerfile.app`은 package를 포함하지만 공개 CLI의 OCI 계약 label과
  `--help`·`--version` smoke check가 없다.
- `Autoresearch`의 shard 함수에는 shard 0 전용 삭제 동작이 없다. 이번 구현은
  이 성질을 회귀 테스트로 고정한다. `Autoresearch-airflow` wrapper의 shard 0
  삭제 코드는 후속 Airflow 이슈에서 제거한다.

## 범위

### 포함

- 액션 로그 공개 module CLI와 공통 실행 결과 형식
- CLI 인자 문법·범위·조합·GCS 경로 검증
- 후보 구성 비율 합계 검증의 도메인 계층 적용
- single과 merge의 skip/overwrite 및 final 게시 순서
- single과 shard의 선택적 격리 상세 파일 best-effort 게시
- manifest 집계만 사용하는 merge 격리 비율 판정
- shard 역할 동일성 회귀 테스트
- Docker image 계약 label과 CLI smoke check
- 액션 로그 실행 문서 갱신

### 제외

- YouTube 일일 수집·backfill 공개 CLI 구현
- `Autoresearch-airflow`의 DAG, KPO, wrapper, Helm values 변경
- shard 0 삭제 동작의 Airflow 저장소 제거
- GCS·GAR·GKE·IAM·Secret Manager 등 인프라 생성
- application image의 실제 GAR push와 운영 digest 교체
- action-log Parquet schema와 prompt schema 변경
- 동일 partition에 대한 동시 writer를 막는 분산 lock
- legacy `--bucket` alias 구현. 필요 여부는 Airflow 전환 이슈에서 별도로
  결정한다.

## 전제와 설계 결정

1. PR #126 병합 후 구현 브랜치를 `main` 기준으로 변경했으며, 구현 PR도
   `main`을 base로 연다.
2. 공개 계약은 module 실행 경로이며 `pyproject.toml` console script는 추가하지
   않는다.
3. 공개 CLI는 계약대로 `gs://bucket/path`를 요구한다. 기존 도메인 함수는 빠른
   단위·통합 테스트를 위해 로컬 경로와 주입 filesystem 지원을 유지한다.
4. CLI는 인자 파싱·실행 결과 직렬화만 담당한다. 후보 생성, 격리 판정,
   manifest 검증과 final 게시 판단은 `autoresearch.action_logs`에 둔다.
5. 최종 결과 보존 계약은 기존 final의 사전 삭제를 금지하고, 검증된 임시
   산출물을 마지막 단계에서 게시하는 것으로 구현한다. 동일 partition 동시
   writer의 순서 보장은 이번 범위에 포함하지 않는다.
6. 상세 격리 JSONL은 품질 판정의 입력이 아니다. 품질 판정은 single 실행
   결과나 shard manifest의 `total_work`, `quarantine_count`를 사용한다.
7. 격리 파일 게시 예외는 무시하지 않는다. warning 로그와
   `quarantine_publish_failed` JSON event에 필요한 안전한 메타데이터를 반환한다.
8. prompt, raw LLM request·response, API key, persona와 user 식별자는 CLI event와
   오류 메시지에 포함하지 않는다.
9. shard의 `--overwrite`는 draft와 manifest를 다시 게시한다는 뜻이다. 같은
   fingerprint의 immutable checkpoint는 삭제하지 않고 재사용한다. 기존 draft와
   유효한 manifest가 모두 있고 overwrite가 false면 해당 shard를 `skipped`로
   처리한다.

## 구현 순서

### Step 1 — 후보 구성 비율 검증을 단일 규칙으로 고정

대상:

- `autoresearch/action_logs/schema.py`
- `tests/test_action_logs_pipeline.py`
- 신규 CLI 테스트

작업:

- personalized, popular, exploration 비율의 합을 검증하는 작은 공용 검증
  함수를 `schema.py`에 둔다.
- `math.isclose(total, 1.0, rel_tol=0.0, abs_tol=1e-9)`와 동등한 규칙을
  사용한다.
- `EventGenerationRequest`의 model-level validator가 이 규칙을 호출하게 해
  CLI를 우회한 Python 호출도 같은 계약을 지키게 한다.
- CLI는 파일 접근이나 generator 생성 전에 같은 규칙으로 조합을 검증한다.
  숫자로 파싱됐지만 합계가 잘못된 경우 `job_summary(status=failed)`를 출력하고
  exit 2로 종료한다.
- 각 비율의 범위 오류, `NaN`, 양·음의 무한대도 실행 전 인자 오류로 처리한다.

경계 테스트:

- `0.7 + 0.2 + 0.1` 성공
- 합계가 `1e-9` 이내인 값 성공
- 합계가 `1e-9`를 초과해 벗어난 값 실패
- 각 값의 `0`, `1`, 범위 밖 값과 non-finite 값
- CLI 조합 오류가 generator와 filesystem에 접근하기 전에 exit 2

### Step 2 — 액션 로그 공개 module CLI 추가

대상:

- 신규 `autoresearch/jobs/__init__.py`
- 신규 `autoresearch/jobs/action_log.py`
- 신규 `tests/test_action_log_job.py`

작업:

- `argparse` 기반으로 `--mode single|shard|merge`와 계약 문서의 액션 로그
  인자를 구현한다.
- mode별 필수 인자와 조합을 실행 전 검증한다.
  - single/shard: YouTube, virtual-user, output 경로와 생성 파라미터
  - shard: `0 <= shard_index < shard_count`, progress/checkpoint 경로
  - merge: shard output, final output, shard count, 품질 임계치
- 날짜는 `YYYY-MM-DD`만, 공개 경로는 정규화된 `gs://bucket/path`만 허용한다.
  빈 경로 요소, `.`·`..`, 중복 separator를 거부한다.
- GCS 입력에 대해 한 번 생성한 `pyarrow.fs.GcsFileSystem`을 도메인 함수에
  주입한다. secret은 기존 환경 변수 소비자만 읽고 CLI 인자로 추가하지 않는다.
- stdout은 한 줄당 하나의 JSON object만 출력한다. warning event를 먼저
  출력하고 마지막에 `job_summary`를 출력한다.
- 종료 코드는 다음 경계로 나눈다.
  - 0: `succeeded`, 기존 final로 인한 명시적 `skipped`
  - 1: 입력 데이터, schema, 품질 임계치, 외부 API와 runtime 실패
  - 2: CLI 문법·type·범위·조합 오류
- `--help`는 사용법을 출력하고 exit 0, `--version`은
  `batch-contract-v1`과 image revision을 출력하고 exit 0으로 종료한다.
- traceback과 일반 logging은 stderr로 제한하고 stdout JSON stream을
  오염시키지 않는다.

테스트:

- mode별 정상 인자 mapping을 monkeypatch한 runner로 검증
- 누락·잘못된 날짜·경로·shard topology·비율 조합의 exit code
- 성공, skipped, runtime 실패의 마지막 `job_summary`와 stdout JSONL 파싱
- warning event가 summary보다 앞에 위치하는지 검증
- 오류 출력에 secret·prompt·raw response·식별자가 없는지 검증
- `--help`, `--version` exit 0

### Step 3 — 선택적 격리 파일을 정상 결과와 분리

대상:

- `autoresearch/action_logs/daily.py`
- `autoresearch/action_logs/schema.py`
- `tests/test_action_logs_daily.py`

작업:

- 선택적 격리 파일 복사를 담당하는 작은 helper를 추가한다.
- helper는 성공 여부와 식별자가 없는 warning metadata를 반환하고, 실패 시
  `logger.warning(..., exc_info=True)`로 원인을 남긴다.
- single 성공 경로는 격리 상세 게시를 best-effort로 시도한 뒤 final
  Parquet을 게시한다. 격리 게시 실패만으로 실행을 실패시키지 않는다.
- single 생성 자체가 실패한 경로에서도 격리 게시 실패가 원래
  `ActionLogGenerationError`를 가리지 않게 한다.
- shard는 draft와 manifest를 정상적으로 게시하고, 선택적 shard 격리 파일
  게시 실패는 warning으로만 반환한다. manifest의 `quarantine_count`와 실패
  유형 집계는 상세 파일 저장 여부와 무관하게 유지한다.
- `ActionLogShardManifest`에 error type별 count를 담는 선택적 호환 필드를
  추가하고 새 shard writer는 항상 기록한다. 필드가 있으면 각 count의 합이
  `quarantine_count`와 일치하는지 검증한다. 필드가 없는 기존 v1 manifest는
  전환 기간에 계속 읽을 수 있으므로 `manifest_version`은 올리지 않는다.
- merge에서 `shard_quarantine_base_path`, 최종 `quarantine_base_path`와 상세
  JSONL 재병합 의존성을 제거한다. 전역 격리 비율은 manifest의 집계만으로
  계산한다.
- 기존 Python 호출자 검색 결과와 테스트를 갱신하고, 제거되는 내부 인자가
  외부 공개 API가 아님을 PR에 명시한다.

테스트:

- single 격리 게시 실패에도 final 생성과 성공 summary 유지
- shard 격리 게시 실패에도 draft·manifest 생성과 warning 반환
- manifest error type 집계 합과 `quarantine_count` 불일치 거부
- 실패한 single의 격리 게시 오류가 원래 생성 오류를 대체하지 않음
- shard 상세 격리 파일이 없어도 manifest 집계로 merge 성공·실패 판정
- merge가 shard 격리 JSONL을 읽거나 최종 격리 JSONL을 만들지 않음

### Step 4 — 마지막 정상 Parquet 보존과 최종 게시 순서 구현

대상:

- `autoresearch/action_logs/daily.py`
- `tests/test_action_logs_daily.py`

작업:

- single과 merge에 `overwrite: bool = False` 계약을 추가한다.
- final이 이미 있고 overwrite가 false면 입력 로드·LLM 호출·shard 병합 전에
  `status=skipped`를 반환한다.
- shard는 유효한 기존 draft와 manifest가 모두 있고 overwrite가 false면
  `status=skipped`를 반환한다. overwrite가 true면 output을 다시 게시하되 같은
  fingerprint의 checkpoint part는 재사용한다.
- overwrite가 true여도 기존 final을 사전 삭제하지 않는다.
- 새 Parquet은 먼저 임시 경로에 완전히 생성하고 다시 열어 schema와 partition
  날짜를 검증한다.
- local filesystem은 final과 같은 디렉터리의 고유 임시 파일을 쓴 뒤
  `Path.replace()`로 교체한다.
- 주입 filesystem은 고유 staging object에 업로드한 뒤 final 게시 작업을
  마지막으로 수행한다. staging 정리는 성공·실패 모두 시도하되, 정리 실패가
  원래 게시 결과를 가리지 않게 warning으로 남긴다.
- final 게시 helper는 기존 final을 직접 삭제하지 않는다. 게시 단계 자체가
  실패했을 때 기존 final의 byte 내용이 유지되는지를 local 및 fake filesystem
  테스트로 고정한다.
- merge 순서를 다음으로 고정한다.
  1. 모든 manifest 존재·topology·fingerprint 검증
  2. 모든 shard draft 로드·schema 검증
  3. manifest 집계 기반 전역 격리 비율 판정
  4. global CTR normalization·event ID 확정
  5. 최종 event schema·partition 검증
  6. 검증된 Parquet staging
  7. canonical final path 게시

테스트:

- final 존재 + overwrite false → skipped, generator/manifest 로드 미호출
- shard output·manifest 존재 + overwrite false → skipped, generator 미호출
- shard overwrite true → checkpoint 재사용 후 draft·manifest 재게시
- final 존재 + overwrite true + 생성/manifest/품질/schema 실패 → 기존 bytes 유지
- final 존재 + overwrite true + publish 실패 → 기존 bytes 유지
- overwrite true + 성공 → 새 Parquet으로 교체
- 새 partition 성공 → final 생성
- 실패 run에서 이전 final이 남아 있어도 summary/exit code는 실패

### Step 5 — 모든 shard의 역할 동일성을 회귀 테스트로 고정

대상:

- `tests/test_action_logs_daily.py`

작업:

- 같은 사용자 snapshot과 설정으로 shard 0과 다른 shard를 실행해 각 shard가
  자신의 연속 사용자 구간, draft, manifest, checkpoint, progress만 다루는지
  검증한다.
- 모든 manifest가 동일한 `input_fingerprint`와 호환 가능한
  `config_fingerprint` 계약을 갖는지 검증한다.
- 어느 shard도 final action-log 경로의 기존 sentinel 파일을 삭제·변경하지
  않는지 검증한다.
- `max-users cap → input fingerprint → shard selection` 순서와 전체 shard의
  사용자 합집합·비중복성을 검증한다.
- shard index별 조건 분기가 final artifact 관리 동작으로 다시 들어오지 않도록
  repository contract test를 추가한다. 구현 세부 문자열 검색보다 관찰 가능한
  파일 접근·결과를 우선 검증한다.

### Step 6 — image 계약과 운영 문서 갱신

대상:

- `Dockerfile.app`
- `.github/workflows/ci.yml`
- `docs/guides/action-log.md`

작업:

- `Dockerfile.app`에 source, revision, batch-contract version OCI label을 추가한다.
- build argument 또는 환경 변수로 image revision을 CLI `--version`에 전달하고,
  로컬 기본값은 명시적인 `unknown`으로 둔다.
- CI image smoke check에 package import 외 다음을 추가한다.
  - `python -m autoresearch.jobs.action_log --help`
  - `python -m autoresearch.jobs.action_log --version`
- README에 공개 명령 예시, exit code, overwrite, 선택적 격리 파일과
  마지막 정상 결과 보존 규칙을 기록한다.
- 의존성 추가가 없으므로 `pyproject.toml`, `uv.lock`, `requirements.txt`는
  변경하지 않는다. 구현 중 새 의존성이 필요해지면 범위를 재검토한다.

## 테스트 및 검증 순서

가장 좁은 테스트부터 실행한다.

```bash
uv run python -m pytest tests/test_action_log_job.py -v
uv run python -m pytest tests/test_action_logs_pipeline.py \
  tests/test_action_logs_daily.py -v
uv run python -m pytest -v
uv run ruff check autoresearch tests
git diff --check
docker build \
  --build-arg VCS_REF="$(git rev-parse HEAD)" \
  -f Dockerfile.app -t autoresearch:batch-contract-v1 .
docker run --rm autoresearch:batch-contract-v1 \
  python -m autoresearch.jobs.action_log --help
docker run --rm autoresearch:batch-contract-v1 \
  python -m autoresearch.jobs.action_log --version
```

실제 GCS와 외부 LLM을 사용하는 테스트는 pytest에 넣지 않는다. 운영 전환
단계에서 별도의 QA run으로 다음을 확인한다.

- 1,000명·5개 shard 실행
- 모든 shard의 동일 input/config fingerprint
- 상세 격리 파일 게시 실패 주입 후 final 성공
- merge 품질 실패 후 이전 final 보존
- 성공 run의 마지막 stdout event가 `job_summary`

로컬 구현 검증 결과(2026-07-13): 전체 pytest 278개, 변경 파일 대상 Ruff,
`uv lock --check`, runtime requirements mirror, proxy export drift와
`git diff --check`가 통과했다. Python 3.12 application image를 빌드하고 기본
실행, action-log `--help`·`--version`, runtime import, OCI label과 non-root user를
확인했다.

## 커밋 분리 제안

구조 변경과 동작 변경을 분리한다.

1. `feat: 액션 로그 공개 batch CLI 추가`
2. `fix: 격리 산출물과 final 게시 성공을 분리`
3. `test: shard 역할과 마지막 정상 결과 보존 검증`
4. `chore: batch image 계약 smoke check 추가`
5. `docs: 액션 로그 공개 실행 방법 갱신`

실제 diff가 작으면 테스트는 대응 동작 커밋에 포함할 수 있으나, 무관한 포맷
변경이나 Airflow 구조 변경은 섞지 않는다.

## 롤아웃과 롤백

1. 구현 PR은 #126 위의 스택형 PR로 검증한다.
2. #126 병합 후 구현 PR base를 `main`으로 바꾸고 전체 CI를 다시 실행한다.
3. 구현 PR 병합 후 application image를 immutable digest로 build·push한다.
4. image의 `--help`, `--version`과 소규모 action-log QA를 실행한다.
5. 후속 `Autoresearch-airflow` 이슈에서 KPO를 새 공개 CLI와 image digest로
   전환하고 shard 0 특수 삭제와 격리 재병합 wrapper를 제거한다.
6. 문제가 생기면 Airflow가 이전 image digest를 다시 사용한다. 실패한 새
   실행은 기존 final을 보존하므로 데이터 파일 복원 작업을 기본 롤백 절차로
   요구하지 않는다.

## 주요 리스크와 대응

| 리스크 | 대응 |
| --- | --- |
| public CLI와 Python 함수의 검증 규칙 불일치 | 비율 합계 규칙을 `schema.py`의 단일 helper로 공유하고 양쪽 테스트 |
| 격리 저장 실패를 숨겨 진단 정보 상실 | warning 로그와 JSON event, manifest 집계 유지 |
| overwrite 재실행이 기존 정상 파일 훼손 | 사전 삭제 금지, staging 후 final 게시, 실패 주입 테스트 |
| shard 상세 파일 제거로 품질 판정 변화 | manifest의 total/quarantine count만 사용하고 기존 전역 임계치 테스트 유지 |
| stdout에 일반 로그나 민감정보 혼입 | JSON event writer만 stdout 사용, logging/traceback은 stderr, redaction 테스트 |
| 기존 DAG와 공개 CLI가 동시에 운영됨 | 이번 PR은 app 기능만 추가하고 Airflow cutover 전 기존 DAG 동작 유지 |
| 동일 partition 동시 overwrite 경쟁 | 이번 범위의 비목표로 명시하고 Airflow Pool·run 정책에서 단일 writer 보장 |

## 완료 체크리스트

- [x] 공개 action-log module CLI와 mode별 인자 계약 구현
- [x] 비율 합계 허용 오차 및 exit 2 검증
- [x] single/shard 격리 상세 파일 best-effort 게시
- [x] merge의 shard 격리 JSONL 의존성 제거
- [x] skip/overwrite와 마지막 정상 final 보존 구현
- [x] shard 0 포함 역할 동일성 테스트
- [x] JSONL event·exit code·민감정보 차단 테스트
- [x] 전체 pytest·Ruff·`git diff --check` 통과
- [x] Docker build·`--help`·`--version` smoke check 통과
- [x] 액션 로그 README 갱신
- [x] Airflow·인프라 변경이 diff에 포함되지 않음
