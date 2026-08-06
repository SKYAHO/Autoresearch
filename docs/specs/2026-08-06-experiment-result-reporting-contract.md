# 실험 판정 결과 Experiment API 반영 계약

## 목적

`compare-paired-experiment`가 만든 `PairedExperimentResult` JSON을 읽어, 그 판정을
Experiment API(PostgreSQL)에 반영해 Streamlit 워크벤치에서 보이게 한다. 실행도 판정도
하지 않는다 — **이미 내려진 판정을 옮기는 것**이 이 계약의 전부다.

구현 이슈는 `#550`이다.

## 배경 — 지금 두 기록 경로가 서로 남남이다

paired 판정 결과가 실제로 소비되는 경로는 `auto-research-promotion.yml` +
`promotion_gate.py` → GitHub PR 코멘트 하나뿐이다. 이 경로는 승격 게이트 전용이고
Postgres에 아무것도 쓰지 않는다. 반대로 Experiment API(`agent_orchestration`)와
워크벤치는 완비되어 있지만, 거기에 판정 결과를 넣어주는 코드가 `src/`·`autoresearch/`
어디에도 없다.

그 결과 "가설 → 실험 → 결과를 대시보드에서 본다"는 최소 흐름이 마지막 한 칸에서
끊긴다. 이 spec은 그 한 칸만 잇는다.

## 범위

- `src/cli.py`에 `report-experiment-result` 서브커맨드 신설
- `agent_orchestration/ui/client.py`의 `ExperimentClient`에 쓰기 메서드 2개 추가
  (`patch_status`, `post_log`)
- 위 모듈 docstring의 책임 범위 갱신

## 비범위

- Airflow DAG, 조건별 이미지 빌드, `PairedExperimentRequest` 조립 — 별도 트랙이다.
  특히 request 조립은 실행 산출물(fingerprint류)에 의존해 이 계약보다 훨씬 크다.
- 승격(`POST /experiments/{id}/promote`) 호출
- `ExperimentStep` 기록 — 이 명령은 실행 도중이 아니라 실행이 끝난 뒤 한 번 돌아서
  표현할 진행 단계가 없다

## CLI 계약

```
uv run python -m src.cli report-experiment-result \
  --result <paired-result.json> \
  --experiment-id <uuid> \
  [--log-uri gs://...]
```

- `--result`: `compare-paired-experiment --output`이 게시한 JSON 경로.
  `PairedExperimentResult.model_validate`로 검증하며, 실패하면 API를 호출하지 않는다.
- `--experiment-id`: 대상 실험 UUID. 결과 payload의 `experiment_id`는 `#454`의 실험
  식별자로 Postgres UUID와 **다른 좌표**이므로 겹쳐 쓰지 않고 별도 인자로 받는다.
- `--log-uri`: 실행 로그 위치를 알고 있으면 포인터 로그에 포함한다(선택).

접속 정보는 `ExperimentClient.from_environment()`가 읽는 기존 환경변수
(`ORCH_UI_API_BASE_URL`, `ORCH_UI_API_TOKEN`)를 그대로 쓴다. 새 환경변수를 만들지
않는다.

### 종료 코드

`compare-paired-experiment`의 관례를 따른다.

| 코드 | 조건 |
| --- | --- |
| 0 | 목표 터미널 상태까지 반영 완료(이미 그 상태여서 아무것도 안 한 경우 포함) |
| 1 | API 호출 실패, 허용되지 않는 전이, 덮어쓰기 거부 |
| 2 | 인자 오류, 결과 JSON 계약 위반 |

에러 출력에 예외 원문을 싣지 않는다. payload에 URI·식별자가 섞여 있고 클라이언트가
토큰을 보유하므로, 기존 CLI와 같이 예외 **타입과 고정 진단**만 출력한다.

## 상태 전이 계약 — 재개 가능한 자가 claim

### 시작 시 현재 상태를 먼저 읽는다

`PATCH /experiments/{id}/status`는 **멱등이 아니다**. `update_experiment_status`가
`idempotency_key=f"status-update:{uuid.uuid4()}"`와 `check_idempotency=False`로
전이하므로(`agent_orchestration/app/experiments/service.py:268-290`), 같은 전이를 두 번
호출하면 event 행이 두 개 쌓인다.

따라서 이 명령은 "CREATED에서 시작한다"고 가정하지 않는다. 실행 시작 시
`GET /experiments/{id}`로 현재 상태를 읽고 **목표 터미널까지 남은 전이만** 밟는다.

| 현재 상태 | 밟는 경로 |
| --- | --- |
| `CREATED` | **전이하지 않고 종료 코드 1** — launcher가 선점할 대기 행이다 |
| `RUNNING` | `EVALUATING` → 터미널 |
| `EVALUATING` | 터미널 |
| 목표 터미널과 동일 | 전이 없이 성공 종료 |
| 다른 터미널 / `PROMOTED` | **전이하지 않고 종료 코드 1** |

마지막 행이 핵심이다. 이미 결론이 난 실험을 다른 결론으로 덮어쓰는 것은 조용한 데이터
손상이므로, 재시도 편의보다 우선해 막는다.

첫 행은 `#547` 병합에 따라 뒤집힌 결정이다. 근거는 아래 `알려진 한계`에 있다.

### 주차된 RUNNING을 만들지 않는다

중간 전이 후 예외가 발생하면 `ERROR`로 내려 터미널을 보장한다. `RUNNING→ERROR`와
`EVALUATING→ERROR`는 모두 허용 전이다(`models.py:77-93`).

프로세스가 `ERROR` 강등조차 못 하고 죽으면 `RUNNING`에 남는다. 이 계약은 그 창을
없애지 못하며, **같은 명령의 재실행으로 회복**시킨다 — 위 표의 `RUNNING` 행에서
재개된다. "고아를 구조적으로 제거한다"가 아니라 "고아가 자동 회복된다"가 이 설계의
정확한 성질이다.

### 전이 사유는 event로 남는다

`PATCH /status`가 내부적으로 event 행을 만들므로(`service.py:280-289`) 별도
`POST /events` 호출 없이 판정 사유가 타임라인에 남는다. 이 계약은 event를 따로
만들지 않는다.

초안 단계에서는 `CREATED→RUNNING` 자가 claim을 허용하고 `manual-self-claim:` 접두사로
감사 가능하게 두려 했다. `#547` 병합으로 그 전이 자체를 하지 않게 되어 접두사도
사라졌다 — 아래 `알려진 한계` 참고.

## 결과 → 상태 매핑

`PairedExperimentResult.outcome`은 3값이다(`src/pipeline/paired_experiment.py:178`).

| outcome | 실험 상태 |
| --- | --- |
| `comparison_passed` | `PASSED` |
| `comparison_rejected` | `FAILED` |
| `comparison_failed` | `ERROR` |

`comparison_failed`를 `FAILED`가 아니라 `ERROR`로 보내는 것이 이 표의 유일한 판단
지점이므로 근거를 남긴다.

`comparison_failed`는 두 가지 서로 다른 상황을 겸한다. 하나는 `EvaluationVerdict.HOLD`
판정이고(`:66-68`), 다른 하나는 요청·lineage 검증 실패로 **판정 엔진을 부르지도 못한**
경우다(`:445, :466, :478`). 둘 다 "기각"이 아니라 "판정되지 않았다"에 해당하므로
`FAILED`로 옮기면 대시보드에서 REJECT와 구분되지 않는다.

이 둘을 리포터가 다시 갈라내는 선택지는 **채택하지 않는다**. 기계적 분기가 성립하지
않기 때문이다.

- `EvaluationReasonCode`와 `PairedExperimentReason`은 별개 Enum이지만 값이 겹친다
  (`seed_policy_mismatch`가 양쪽에 존재). 문자열만으로 출신을 판별할 수 없다.
- `EvaluationReasonCode`는 HOLD 전용이 아니다. ELIGIBLE·REJECT 사유가 같은 Enum에
  섞여 있어, 출신을 안다 해도 HOLD 여부가 결정되지 않는다.

남는 방법은 사람이 만든 분류표뿐이고, 그것은 판정 책임을 리포터로 끌어들인다. 더구나
HOLD와 검증 실패를 하나로 합친 것은 사고가 아니라 `#454`가 의도적으로 내린 결정이다
(`:63-65` — "`hold`(판정 불가)는 성공이 아니다"). 상위 계약이 합친 구분을 하위
리포터가 되돌리는 것은 "옮기기만 한다"는 경계를 깬다.

구분 정보는 `reason`과 `metric_snapshot.reason_codes`에 원본 그대로 실리므로 손실되지
않는다.

## 기록 내용

### `metric_snapshot`

`StatusUpdateRequest.metric_snapshot`은 자유 `dict`다(`schemas.py:99`). 여기에 결과
계약의 필드를 **이름을 바꾸지 않고** 옮긴다.

`metric_name`, `primary_baseline`, `primary_candidate`, `paired_delta_mean`,
`confidence_interval_lower`, `confidence_interval_upper`, `seeds`, `outcome`,
`reason_codes`, `evaluated_at`

필드명을 새로 짓지 않는 이유는 `#454` 계약이 바뀌면 이 spec도 같이 깨져서 드러나게
하기 위해서다. 이름을 갈아끼우면 계약 변경이 조용히 통과한다.

guardrail 지표는 `PairedExperimentResult`에 **존재하지 않는다**. 이 계약은 없는 지표를
만들어 넣지 않는다.

이 값이 대시보드까지 도달하는 경로는 확인되어 있다. `_transition_experiment`가
`metric_snapshot`을 그대로 `Experiment.metric_summary`에 대입하고
(`service.py:252-253`), 워크벤치 결과 탭이 그 필드를 렌더한다
(`ui/views.py:224, :312`).

### `reason`

`decision_reason`과 `reason_codes`를 합쳐 넣는다. `reason`은 8192자 상한이 있으므로
(`schemas.py:98`) 초과 시 잘라내되, **잘렸다는 사실을 문자열 안에 남긴다**.

### `POST /logs` — 원본이 아니라 포인터

`ExperimentLogCreate.content`는 8192자 상한이다(`schemas.py:142`). seed 여러 개의
paired 결과 JSON은 이 한도를 넘길 수 있으므로 **`result.json` 원본을 넣지 않는다.**

대신 포인터 로그 한 건만 남긴다: `runs[].log_uri`, `runs[].artifact_uri`,
`model_uri`, `--log-uri`로 받은 값, 그리고 outcome 요약. 원본은 이미 GCS에 있고,
Postgres는 그 위치를 가리키기만 한다.

`idempotency_key`는 결정론적으로 만들어 재실행 시 로그가 중복되지 않게 한다. 다만
**식별자를 그대로 이어붙이면 128자 상한을 넘는다**(`schemas.py:140`).

| 조각 | 길이 |
| --- | --- |
| `experiment_id` (UUID) | 36 |
| `:paired-result:` | 15 |
| `evaluation_id` = `"experiment-evaluation-"` + sha256 hex | 86 |
| `evidence_id` = `"paired-seed-evidence-"` + sha256 hex | 85 |

`evaluation_id`를 쓰면 137자, `evidence_id`를 쓰면 136자로 둘 다 상한을 넘겨
`POST /logs`가 Pydantic 검증에서 거부된다. `_stable_id`의 접두사는 고유성에 기여하지
않는 장식이므로(`experiment_evaluation.py:385-387`), **마지막 `-` 뒤 sha256 부분만**
쓴다.

```python
raw = result.evaluation_id or result.evidence_id or result.candidate_sha
idempotency_key = f"{experiment_id}:paired-result:{raw.rsplit('-', 1)[-1]}"
```

sha256 hex는 64자이므로 최대 115자, `candidate_sha` fallback은 91자로 항상 상한 안에
들어온다. **이 길이 계산은 테스트로 고정한다** — 상한을 넘기면 런타임에야 드러나는
종류의 실패다.

## 클라이언트 확장

`ExperimentClient`에 `patch_status`, `post_log` 두 메서드를 추가한다. 필요한 것은 이
둘뿐이다 — 판정 event는 `PATCH /status`가 내부에서 만들므로 `POST /events` 메서드는
만들지 않는다.

`_request_json(method, path, ...)`이 method를 그대로 전달하므로
(`client.py:228, :246`) 인증(`X-Orch-Token`)·에러 변환 계층은 그대로 재사용된다.

이 모듈은 현재 "Streamlit이 쓰는 클라이언트"로 문서화되어 있고 `[비책임]`에 "상태·
Event·Log 쓰기"가 명시되어 있다. 같은 커밋에서 그 항목을 삭제하고 책임 범위를
갱신한다 — 코드가 문서를 벗어난 채로 두지 않는다.

## 구현 시 함정

### `metric_snapshot`은 터미널 전이에서만 싣는다

`_transition_experiment`는 `metric_snapshot`이 `None`이 아니면 **전이할 때마다**
`Experiment.metric_summary`를 통째로 덮어쓴다(`service.py:252-253`). 중간 전이
(`→RUNNING`, `→EVALUATING`)에 스냅샷을 실으면 최종 값이 확정되기 전의 내용이
대시보드에 잠깐 노출되고, 중간에 프로세스가 죽으면 그 상태로 남는다.

따라서 중간 전이는 `reason`만 보내고 `metric_snapshot`은 `None`으로 둔다. 지표는
**터미널 전이 한 번에만** 싣는다.

### 설정 가드를 다시 만들지 않는다

`ExperimentClient.__init__`이 `base_url`과 `api_token`이 비어 있으면
`ApiConfigurationError`를 던진다(`client.py:72-77`). `from_environment()`의 토큰
기본값이 빈 문자열이지만, client가 만들어지지도 API가 호출되지도 않는다 — **이미
fail-closed다.**

따라서 이 명령은 토큰을 사전 검사하지 않는다. `ExperimentClient.from_environment()`를
`ApiConfigurationError`로 감싸 **종료 코드 2에 매핑하기만** 한다. 같은 검사를 CLI에
한 벌 더 두면 동작은 같아도 왜 있는지 설명할 수 없는 중복이 남는다.

## 알려진 한계

### 자가 claim은 (A)로 강등했다 — `#547` 병합으로 조건 충족

이 절은 원래 "머지되면 강등한다"는 **예정**이었다. 계획 작성 몇 시간 뒤
`e81d7f7 feat: 실험 브랜치 Bootstrap Kubernetes Job Phase 1 (#547)`이 main에 병합되어
조건이 충족되었고, **구현에 반영했다**. 아래는 그 근거이며 이제 과거형이 아니라 현행
계약이다.

`0004_experiment_branch_bootstrap` migration이 `executor_job_name`을 신설했고
`agent_orchestration/launcher/repository.py`가 함께 들어왔다. 같은 날
`Autoresearch-infra`에도 launcher CronJob Terraform이 착지 중이라, 코드 레벨 가능성이
아니라 **운영 경합이 임박한 상태**다.

| launcher 쿼리 | 조건 |
| --- | --- |
| `CREATED_CLAIM_STATEMENT` | `status==CREATED` AND `executor_job_name IS NULL` |
| `RECOVERABLE_CLAIM_STATEMENT` | `status==RUNNING` AND `executor_job_name IS NOT NULL` AND `executor_job_created_at IS NULL` |

자가 claim으로 만든 `RUNNING` 행은 `executor_job_name`이 `NULL`이라 두 쿼리 어디에도
걸리지 않는다. 다만 launcher는 `CREATED`와 `RUNNING`만 보므로, **터미널에 도달한 행은
영향받지 않는다.** 이 계약이 "주차된 RUNNING을 만들지 않는다"를 불변식으로 두는 이유가
이것이다.

`CREATED` + `executor_job_name IS NULL`은 이제 **launcher가 정당하게 집을 대기 행**이다.
따라서 이 명령은 **`CREATED`를 만나면 전이 없이 종료 코드 1로 거부한다.** 경로를
삭제한 것이 아니라 허용 범위를 `RUNNING`/`EVALUATING`으로 좁힌 것이다.

### 이 강등의 대가 — 수동 실행 경로가 좁아졌다

`(C)` 스코프를 고른 이유 중 하나가 "launcher 없이도 오늘 수동으로 검증 가능"이었는데,
그 편의는 사라졌다. 이제 실험을 `RUNNING`까지 올리는 주체는 launcher뿐이다.

이것은 손실이 아니라 **성격 변화**다. launcher가 매 분 `CREATED`를 선점하는 지금,
사람이 손으로 `CREATED→RUNNING`을 만드는 것은 "없어서 아쉬운 편의"가 아니라 "있으면
위험한 경합"이다. 수동 검증이 필요하면 launcher가 올려준 `RUNNING` 실험을 대상으로
한다.

### experiment_id 좌표가 둘이다

`PairedExperimentResult.experiment_id`(#454 실험 식별자)와 Postgres `Experiment.id`
(UUID)는 다른 좌표계다. 이 명령은 둘을 별도 인자로 받으며 **연결이 맞는지 검증할
방법이 없다.** 잘못 짝지으면 엉뚱한 실험에 결과가 기록된다. 두 좌표를 잇는 계약은 이
spec의 범위 밖이며, 그때까지는 호출자 책임이다.

### 대시보드 표시는 raw JSON이다

워크벤치의 결과 탭은 `metric_summary`를 JSON으로 렌더한다(`ui/views.py`
`_render_metrics`). 이 계약이 채우는 것은 그 원천 데이터이며, 사람이 읽기 좋은
보고서 형식은 별도 작업이다.

## 완료 조건

- `report-experiment-result`가 상태 전이 표의 모든 행을 계약대로 처리한다
- 중간 실패 시 `ERROR`로 강등되어 `RUNNING`에 주차된 행이 남지 않는다
- 같은 명령을 재실행해도 event·log가 중복 생성되지 않는다
- 이미 다른 터미널 상태인 실험을 덮어쓰지 않고 종료 코드 1로 거부한다
- `reason` 8192자 초과 시 잘림 표시와 함께 잘린다
- 중간 전이는 `metric_snapshot`을 싣지 않으며, `metric_summary`는 터미널 전이에서 한 번만 갱신된다
- `ORCH_UI_API_TOKEN` 미설정 시 `ApiConfigurationError`가 종료 코드 2로 매핑된다
- `idempotency_key`가 128자 상한 안에 들어옴을 테스트가 고정한다 (`evaluation_id`·
  `evidence_id`·`candidate_sha` 세 경로 모두)
- `PairedExperimentResult` 계약 위반 JSON은 API 호출 전에 종료 코드 2로 거부된다
- `ExperimentClient` docstring의 `[비책임]`에서 쓰기 항목이 제거되었다
