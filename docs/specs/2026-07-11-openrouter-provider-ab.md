# Spec: OpenRouter provider 자동/고정 라우팅 A/B 계약

- Status: Implemented; local contract verified (live A/B pending)
- Date: 2026-07-11
- Issue: `SKYAHO/Autoresearch#116`
- Owner: Airflow Orchestration (bbungjun)
- 관련 모듈: `autoresearch/action_logs/llm_generator.py`,
  `autoresearch/action_logs/daily.py`

## 배경

동일한 `mistralai/mistral-nemo` 요청에서 OpenRouter 기본 자동 라우팅과 특정
provider 고정 라우팅의 지연·실패율을 비교하려면 두 arm의 요청 payload와
checkpoint namespace가 명확히 분리되어야 합니다. 기존 구현은 ambient
`OPENROUTER_PROVIDER_*` 환경값을 선택적으로 반영하지만, 실험용 자동 arm에서
이 값을 확실히 제거하거나 provider 하나만 고정하는 공개 계약이 없습니다.

또한 OpenRouter 내부 provider fallback과 429는 application retry 로그만으로
구분할 수 없습니다. 공식 router metadata를 요청하되 prompt, response,
metadata 원문을 노출하지 않는 최소 집계가 필요합니다.

## 목표

- `run_daily_action_log(...)`와 `run_daily_action_log_shard(...)`가
  `provider_routing_mode`, `provider_slug`, `expected_user_count`를 공개 인자로
  받습니다.
- 자동 arm은 ambient provider 설정과 무관하게 provider payload를 생략합니다.
- 고정 arm은 정규화한 provider slug 하나만 허용하고 fallback을 끕니다.
- mode, slug, 실제 요청에 반영되는 provider preference를 config fingerprint에
  포함해 arm별 checkpoint를 격리합니다.
- 공식 router metadata에서 선택 provider, router attempt/fallback 수, 내부 429
  수만 안전하게 추출합니다.
- 100-user QA 입력은 shard 분할 전에 정확한 user 수를 검증합니다.

## 범위 밖

- `mistralai/mistral-nemo` 이외 모델로의 전환
- concurrency, retry, timeout, prompt/schema 변경
- 실제 100-user 또는 6,983-user 비용성 benchmark 실행
- 별도 Windows 로컬 기본 checkpoint 부모 경로 결함 수정
- raw prompt/response/router metadata 저장 또는 로깅

## 공개 API 계약

두 daily runner에 다음 keyword-only 인자를 추가합니다.

```python
provider_routing_mode: str = "default"
provider_slug: str | None = None
expected_user_count: int | None = None
```

`provider_routing_mode` 허용값과 payload는 다음과 같습니다.

| mode | `provider_slug` | 요청의 `provider` payload |
| --- | --- | --- |
| `default` | 금지 | 기존 explicit/env preference를 그대로 해석 |
| `auto` | 금지 | 완전히 생략 |
| `fixed` | 필수 | `{"only": [slug], "allow_fallbacks": false}` |

- mode는 허용값 외 입력을 거부합니다.
- fixed slug는 앞뒤 공백과 대소문자를 정규화하고, OpenRouter provider base/variant
  형식(예: `deepinfra`, `deepinfra/turbo`, `google-vertex/us-east5`)만 허용합니다.
- `default`/`auto`에 slug를 주거나 `fixed`에서 slug가 없거나 유효하지 않으면
  API 요청 전에 실패합니다.
- `rule_based` generator는 `default`만 허용하며 `auto`/`fixed`를 명확히
  거부합니다.
- 모든 mode에서 model 이름은 변경하지 않습니다.

## Router metadata 계약

모든 OpenRouter chat completion 요청에 공식
`X-OpenRouter-Metadata: enabled` header를 보냅니다. 성공 응답의
`model_extra["openrouter_metadata"]`는 additive 외부 입력으로 취급하고 알 수
없는 필드나 잘못된 선택 필드는 무시합니다.

`openrouter_request_complete`에는 존재하고 검증된 경우에만 다음 값을 넣습니다.

- `provider`: `endpoints.available[].selected=true`의 안전한 provider 이름. 없으면
  기존 `response.provider` 또는 `model_extra.provider` fallback을 유지합니다.
- `router_attempt_count`: `attempts` 배열 길이 또는 공식 `attempt` 값
- `router_fallback_count`: `max(router_attempt_count - 1, 0)`
- `router_429_count`: 유효한 `attempts[].status == 429` 개수

metadata의 `summary`, `pipeline`, `requested` 및 기타 원문은 로그에 넣지
않습니다. prompt, response content, API key, user/persona 식별자도 기존과 같이
기록하지 않습니다.

## Fingerprint와 입력 크기 계약

- `OpenRouterActionLogGenerator.fingerprint_config`에 정규화된 mode/slug와 실제
  request를 만드는 resolved provider preference를 포함합니다.
- mode 또는 slug가 달라지면 동일 model/input/request 설정이어도 config
  fingerprint와 checkpoint namespace가 달라야 합니다.
- `expected_user_count`는 생성 결과를 바꾸는 설정이 아니라 입력 precondition이므로
  fingerprint에는 넣지 않습니다.
- `expected_user_count`가 주어지면 parquet에서 user를 모두 로드한 직후 실제
  개수와 정확히 비교합니다. 불일치 시 shard 분할·generator 생성·checkpoint
  초기화 전에 `ValueError`로 실패합니다.

## 검증 기준

- default 요청이 기존 ambient preference 동작을 보존합니다.
- auto 요청은 ambient 값이 있어도 `extra_body.provider`가 없습니다.
- fixed 요청 payload가 정확하며 slug가 정규화·검증됩니다.
- invalid mode/mode-slug 조합과 rule-based auto/fixed가 fail closed 합니다.
- mode/slug 변경 시 fingerprint/config가 달라집니다.
- 공식 metadata shape에서 선택 provider, fallback, 429를 추출합니다.
- raw response와 metadata의 민감 본문이 구조화 로그에 없습니다.
- expected user count 100의 일치/불일치 계약을 검증합니다.
- 기존 provider/retry/observability/daily 회귀 테스트와 ruff를 실행합니다.

## 구현 및 로컬 검증 결과 (2026-07-11)

- `tests/test_action_logs_llm_generator.py`,
  `tests/test_action_logs_observability.py`, `tests/test_action_logs_pipeline.py`:
  **67 passed**
- 신규 daily 공개 인자, 100-user precondition, rule-based 거부, fingerprint 계약:
  **11 passed, 18 deselected**
- 전체 pytest: **255 passed, 2 skipped, 12 failed**. 12개 실패는 변경 전 기준선과
  동일한 Windows 로컬 shard checkpoint 부모 경로 `FileNotFoundError`이며, 이 이슈의
  명시적 범위 밖이라 수정하지 않았습니다. 신규 실패는 없습니다.
- `ruff check autoresearch tests`: **passed**
- 추가 `ruff check .`: 소유 범위 밖 `examples/ctr_pipeline_scaffold/`와
  `scripts/generate_and_upload_dummy_data.py`의 기존 9건으로 실패했습니다. 이번
  변경 파일에서는 위반이 없습니다.
- `git diff --check`: **passed** (Windows line-ending 안내만 출력)

실제 OpenRouter 호출과 100-user paired A/B benchmark는 이 구현 검증에 포함하지
않았습니다. Airflow 실험 DAG와 QA GCS prefix를 사용한 비용성 실행에서 별도로
측정해야 합니다.
