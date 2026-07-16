# Spec: Action Log 프롬프트 토큰 절감 (포맷 최적화)

- Status: Implemented; Live QA passed
- Date: 2026-07-10
- Owner: Airflow Orchestration (bbungjun)
- 관련 모듈: `autoresearch/action_logs/` (`llm_generator.py`, `pipeline.py`, `schema.py`)
- 관련 문서: `docs/guides/agent-simulator-spec.md` (출력 스키마 SSOT),
  `docs/guides/action-log.md`

## 배경 / 문제

Action log 생성은 `OpenRouterActionLogGenerator.generate()`가 **유저 1명 ×
후보영상 청크** 단위로 LLM(`mistralai/mistral-nemo`)을 호출한다. 호출마다
system harness + 유저 프로필 + 후보영상 목록 + 출력형식 지시를 전송하며,
후보영상 목록이 입력의 80%+를 차지한다. 출력은 후보마다 전체 키와 video_id를
되풀이한다.

현재 호출당 대략 입력 ~4,100 tok / 출력 ~800 tok (24후보, `chunk_size=0` 기준).
대규모 생성(수만 유저)에서 토큰 비용과 지연이 누적된다.

프롬프트 캐싱 기반 절감은 별도 검토 결과 **이 워크로드에 실익 없음**으로 기각.
정적 접두부(system+지시 ≈ 350 tok)가 provider 캐시 최소 토큰(1,024~4,096)에
못 미치고, 현재 모델이 이미 사실상 최저가($0.02/$0.03)라 캐싱 가능 모델은
7배 이상 비싸다. 따라서 **모델 무관하게 실토큰을 줄이는 포맷 변경**을 채택한다.

## 목표

포맷 변경으로 판정 품질을 유지하면서(would_like 제외) 토큰을 줄인다.

| 지표 | 현재(측정) | 신 포맷(측정) | 비고 |
| --- | --- | --- | --- |
| 입력 tok/호출 (24후보) | 2,669~12,012 | 2,183~11,503 (−4~18%) | 설명 길이 의존 |
| 출력 tok/호출 (24후보) | 845~893 | 316 (−63~65%) | index 포함, 콘텐츠 무관 |

> tiktoken cl100k_base 근사, fixture(짧은 설명)~최대길이 콘텐츠 범위. 실제
> mistral-nemo 토크나이저와 절대값은 다르나 상대 비율은 견고. 초기 스펙의 낙관적
> 목표(입력 −35%, 출력 −75%)는 실측으로 위와 같이 보정됨.

## 범위 (In Scope)

채택한 4개 포맷 절감 방안:

- **방안 2 — 후보 블록 키 반복 제거**: 후보영상을 객체 배열 대신 **위치기반
  배열-of-배열**로 직렬화해 반복 키 문자열(`video_id`/`title`/`tags`/
  `channel`/`description`)을 제거한다.
- **방안 3 — video_id → 인덱스 치환**: 프롬프트에 opaque한 11자 video_id를
  넣지 않고 후보의 배열 위치(0-base index)로 식별한다. 입력·출력 양쪽 절감.
- **방안 5 — 인덱스 배열 응답**: 출력을 `[index, cp, wf]` 배열로 받아 video_id·키
  반복을 제거한다. 각 원소가 자기 index를 실어 재정렬에도 재결합 가능(리뷰 반영).
- **방안 6 — would_like 코드 파생**: LLM 출력에서 `would_like`를 제거하고
  `click_propensity`/`watch_fraction`으로부터 코드에서 결정론적으로 파생한다.

## 범위 밖 (Out of Scope)

- 방안 1 (description 길이 축소/제거) — 판정 품질 A/B 검증이 필요하므로 별도
  작업으로 분리. 본 작업은 필드 truncation 한도(title 120 / tags 8 /
  channel 40 / description 160)를 **변경하지 않는다**.
- 프롬프트 캐싱 / 모델 전환 (기각됨).
- 후보 구성 로직(`candidate.py`), 전역 CTR 정규화, 다운스트림 parquet/warehouse
  스키마.

## 설계

### 입력 포맷 (신규 user prompt)

후보 블록을 **위치기반 배열-of-배열**로 바꾼다. 배열 위치 `i` = 후보 인덱스 `i`.
컬럼 순서는 프롬프트에 명시한다: `[title, tags, channel, description]`.

```
후보 영상(24개, 배열 위치 = 후보 index):
컬럼 순서 = [title, tags, channel, description]
[["제목...",["태그1","태그2"],"채널","설명..."],
 ["제목...",[],"채널","설명..."], ...]
```

- 필드 truncation 한도는 현행 유지(`_candidate_block`).
- 배열-of-배열 JSON을 사용하는 이유: TSV/구분자 방식은 title·description 내
  개행·구분자 주입 위험이 있으나, JSON 배열은 `json.dumps`가 이스케이프를
  보장해 안전하다. 키만 제거하고 구조적 안전성은 유지한다.
- 유저 프로필 블록(`_user_profile_block`)은 현행 유지(호출당 1회, 비중 작음).

### 출력 포맷 (신규 응답 계약)

`response_format={"type":"json_object"}`는 top-level object를 요구하므로 배열을
객체로 감싼다. 각 원소는 `[index, click_propensity, watch_fraction]`이며 `index`는
후보의 0-base 배열 위치다.

```json
{"j": [[0, 0.12, 0.34], [1, 0.0, 0.1], ...]}
```

- `would_like`는 출력하지 않는다(방안 6).
- 각 원소가 자기 `index`를 명시적으로 실어, LLM이 순서를 바꿔 반환해도 파싱이
  `index`로 재결합한다(순수 위치 정렬의 무성 오정렬 리스크 제거 — 코드리뷰 반영).
- `index`는 컴팩트한 정수라 video_id를 되싣는 것보다 토큰이 훨씬 싸다. 출력 절감은
  ~72%에서 ~63%로 소폭 낮아지지만 라벨 무결성을 구조적으로 확보한다.

### would_like 파생 규칙 (방안 6)

코드에서 결정론적으로 파생한다. 파생 헬퍼는 would_like가 세팅되는
`pipeline._build_user_drafts`와 근접하도록 `pipeline.py`(또는 공용 모듈)에
둔다. 초기 임계값(캘리브레이션 대상):

```
would_like = (click_propensity >= T_CLICK) and (watch_fraction >= T_WATCH)
```

- 기본값 후보: `T_CLICK = 0.7`, `T_WATCH = 0.6`.
- **결정 필요**: 임계값은 현행 LLM `would_like` 분포(true 비율)를 근사하도록
  캘리브레이션한다. 구현 단계에서 소량 샘플로 현행 대비 true 비율을 비교해
  확정한다. 임계값은 모듈 상수로 두어 조정 가능하게 한다.

### 파싱 계약 변경 (`_build_user_drafts`)

video_id 매핑(`jmap`) 대신 **응답 내 `index`로 후보에 재결합**한다.

- `data["j"]`를 읽어 각 원소 `[index, cp, wf]`를 `index`로 매핑한다.
- 후보 `i`: `cp, wf = by_index[i]` → `click_propensity=_clamp01(cp)`,
  `watch_fraction=_clamp01(wf)`, `would_like=derive(...)`.
- **index 집합 무결성 (동작 계약 변경)**: `index` 집합이 정확히 `0..n-1`(각 1회)이
  아니면 라벨 무결성을 보장할 수 없어 **`schema_fail`로 격리**한다. 구체적으로
  개수 불일치(`len(j)!=n`)·범위 이탈(`index∉[0,n)`)·중복 index·원소 길이≠3을 모두
  거부한다. (현행 v1은 누락 후보를 비클릭으로 패딩했으나 이는 오정렬을 은폐하므로
  금지.) 세 조건(len==n, 범위 [0,n), 중복 없음)이 성립하면 누락도 함께 배제된다.
- `index`가 정수가 아니면(bool 포함, 정수값 float는 허용) → `schema_fail`.
- `json.JSONDecodeError` → `invalid_json` (현행 동일).

### 스키마 / 버전

- **`PROMPT_VERSION` 범프**: `action_log_ctr_v1` → `action_log_ctr_v2`
  (`schema.py:16`). 프롬프트 포맷이 바뀌므로 필수. 이 값은
  `daily.py:1087`에서 체크포인트 재개 호환성 게이트로 쓰이므로, 범프 시
  구버전 체크포인트는 자동 무효화되어 재생성된다(의도된 동작).
- `ImpressionDraft` 및 `ACTION_LOG_DRAFT_PARQUET_SCHEMA`는 **변경 없음**.
  `would_like`는 여전히 저장되며, 값의 출처만 LLM → 코드 파생으로 바뀐다.
  다운스트림(warehouse, EventLog like 이벤트) 계약은 그대로 유지된다.
- `RuleBasedActionLogGenerator`(fixture)도 신규 출력 포맷(`{"j":[[...]]}`)을
  내도록 맞춘다. would_like는 fixture에서 출력하지 않는다(파싱이 코드 파생).

## 동작 계약 (Behavior Contracts)

1. 신규 프롬프트는 후보를 배열 위치로 식별하고, 응답은 각 원소에 `index`를 실은
   `[index, cp, wf]` 배열로 받아 index로 후보에 재결합한다(순서 무관).
2. 응답 `index` 집합이 정확히 `0..n-1`(각 1회)이 아니면(개수·범위·중복·원소길이
   이상) 해당 청크를 `schema_fail`로 격리한다.
3. `would_like`는 LLM이 아닌 코드가 임계값 규칙으로 결정한다.
4. `click_propensity`/`watch_fraction`의 판정 의미·범위(0~1, 소프트 클램프)는
   현행과 동일하다.
5. parquet/warehouse 출력 스키마와 전역 CTR 정규화는 불변.

## 리스크

- **위치 오정렬 (해소됨, 리뷰 반영)**: 순수 위치 정렬은 모델이 순서를 바꿔
  반환하면 라벨이 무성 오염될 수 있었다. → 출력 각 원소에 `index`를 실어 파싱이
  index로 재결합하고, index 집합이 `0..n-1`이 아니면 격리한다. 순서가 바뀌어도
  올바른 후보에 결합되고, 개수/범위/중복 이상은 격리로 방어(계약 2).
- **would_like 분포 변화 → like 이벤트 볼륨 변화**: `draft.would_like`는
  다운스트림 `_expand_events`에서 **like 이벤트 생성 여부**를 결정한다. 파생
  규칙이 현행 LLM 판단과 달라지면 like 이벤트 발생률이 바뀐다.
  → 임계값 캘리브레이션은 판정 true 비율뿐 아니라 **like 이벤트 볼륨 회귀**까지
  포함해 검증한다.
- **체크포인트 무효화**: `PROMPT_VERSION` 범프 시 `daily.py:1087`이 구버전
  manifest에 대해 **`ValueError`를 raise**한다(조용한 재생성이 아님). 호출부가
  이를 재생성 트리거로 처리하는지 런 실패로 처리하는지는 **구현 시 호출 경로
  확인 필요**. → 배포 타이밍을 진행 중 배치가 없을 때로 조율.
- 작은 모델(mistral-nemo)의 배열-of-배열 준수 안정성. → 격리 비율 모니터링,
  필요 시 `max_quarantine_ratio`로 배치 실패 임계 관리(기존 메커니즘 활용).

## 검증 기준

- 신규 포맷으로 입력/출력 토큰이 목표 근처로 감소(측정).
- `schema_fail` 격리 비율이 현행 수준을 크게 벗어나지 않음.
- would_like true 비율이 캘리브레이션 후 현행과 근사.
- 다운스트림 parquet 스키마/행 수 불변 회귀 없음.

## Claude 리뷰 후속 결정 (2026-07-11)

### 리뷰 범위와 처리 상태

PR #112의 Claude 리뷰 스레드를 기준으로 다음과 같이 처리한다.

1. **응답 재정렬 오정렬 위험** — 후속 커밋에서 반영 완료.
   응답은 `[index, click_propensity, watch_fraction]`을 포함하고,
   `_build_user_drafts()`가 index로 후보를 재결합한다. 개수·범위·중복·원소
   길이 검증 실패는 `schema_fail`로 격리한다.
2. **system harness와 실제 JSON 계약 불일치** — 후속 커밋에서 반영 완료.
   harness는 최상위 객체 `{"j": [[index, cp, wf], ...]}`만 허용한다고 명시하며,
   `would_like`는 출력하지 않는다.
3. **`PROMPT_VERSION` 변경의 진행 중 작업 영향** — 아래 rollout 계약과
   회귀 테스트로 동작을 고정한다.

### v1 → v2 배포 시 계약

- `PROMPT_VERSION`은 config fingerprint 입력이므로 v2 실행은 v1 checkpoint
  part를 재사용하지 않는다. v2는 새 `fingerprint=<config_fingerprint>`
  namespace를 생성하고 v1 part는 기존 namespace에 보존한다.
- v1 manifest와 v2 manifest를 하나의 merge에 섞을 수 없다. merge는 현재
  `PROMPT_VERSION`과 일치하지 않는 manifest를 `ValueError`로 거부한다.
- 운영자는 v2 shard를 모든 index에 대해 완료한 뒤 v2 manifest만 merge한다.
- v1 checkpoint는 삭제하지 않으며, 정리 작업은 별도 보존정책으로 수행한다.

### v2 → v1 롤백 시 계약

- 코드와 `PROMPT_VERSION`을 v1으로 되돌리면 v1 fingerprint namespace가 다시
  선택된다. 기존 v1 checkpoint가 있으면 성공 work를 재사용한다.
- v2 checkpoint와 v2 manifest는 v1 실행에서 재사용되지 않는다. v1 merge는
  v2 manifest를 prompt-version mismatch로 거부한다.
- 롤백은 데이터 변환이나 삭제를 요구하지 않는다. 서로 다른 버전 namespace가
  병존할 수 있고, 없는 버전의 작업은 재계산될 수 있다.

### 운영 절차

1. 진행 중인 shard 생성·merge가 없는 시점에 v2를 배포한다.
2. 새 버전으로 모든 shard를 생성하고 manifest의 `prompt_version`과
   `config_fingerprint`를 확인한다.
3. 모든 shard가 같은 v2 contract를 가질 때만 merge한다.
4. 첫 배치에서 `schema_fail` 비율, click CTR, `would_like` 비율, like event
   수를 기존 기준과 비교한다.
5. 롤백이 필요하면 v1 코드/버전을 복원하고 v1 manifest만 다시 구성한다.

### 검증 기준 추가

- prompt version 변경 시 checkpoint namespace가 분리된다.
- v1 rollback 시 기존 v1 checkpoint를 재사용한다.
- prompt version mismatch manifest는 merge 전에 거부된다.

## Live QA 후속 개선: 출력 계약 준수 안정화 (2026-07-12)

### 재현 증거

개선된 v2 prompt를 실제 dev GCS 입력으로 검증했다.

- 입력 영상:
  `gs://ar-infra-501607-autoresearch-dev-raw-data/data_lake/youtube_trending_kr/dt=2026-07-10/part-0.parquet`
- 영상 200행 중 20행, virtual user 3명, `mistralai/mistral-nemo`, 유저당 1콜.
- 결과: 2콜 정상, 1콜 `invalid_json` 격리.
- 실패 응답은 홀수 index 10개만 반환했고, 최상위 JSON에 닫는 `]`가 하나 더
  있어 `JSONDecodeError`가 발생했다. 문법을 수동 보정하더라도 index 개수 계약
  위반으로 `schema_fail` 대상이다.

이 결과는 validator 누락이 아니라 작은 모델이 compact 예시의 `...`를 일부 항목
생략 허용으로 오해하고 JSON 문법까지 위반할 수 있음을 보여준다. v2는 실패를
안전하게 격리하지만, 유효한 유저 판단을 복구하지는 못한다.

### v3 user prompt 계약

- 추상 예시 `{"j": [[index, cp, wf], ...]}`와 모든 ellipsis를 제거한다.
- 호출 시 후보 개수 `n`에 맞춰 **완전한 유효 JSON skeleton**을 동적으로 만든다.

```json
{"j":[[0,0.0,0.0],[1,0.0,0.0],[2,0.0,0.0]]}
```

- 모델은 skeleton의 객체/배열 구조, row 개수, index, row 순서를 바꾸지 않고 각
  row의 두 placeholder 값만 판단 결과로 교체한다.
- prompt에 `required_indexes` 전체 목록과 `expected_count`를 명시한다.
- 출력 전 자체 검증 항목을 명시한다: 유효 JSON, key는 `j` 하나, row 수 `n`,
  index `0..n-1` 각 1회, 각 row 길이 3, 숫자 범위 0~1.
- user prompt가 바뀌므로 `PROMPT_VERSION`을 `action_log_ctr_v3`로 올린다.

### JSON/schema 교정 재시도 계약

- 최초 응답이 `invalid_json` 또는 `schema_fail`이면 OpenRouter generator에 한해
  같은 유저·후보 청크를 **최대 1회** 다시 요청한다.
- 재시도 prompt는 이전 실패 유형을 안전한 enum 문자열로만 전달하고, 실패 응답
  원문은 prompt에 재주입하지 않는다.
- transport/API 오류는 기존 HTTP retry 정책을 따르며 schema 교정 재시도 대상이
  아니다.
- 두 번째 응답이 성공하면 정상 draft로 처리한다. 다시 실패하면 두 번째 응답과
  최종 오류를 기존 `QuarantineRecord`에 저장한다.
- RuleBased 및 외부 custom generator처럼 교정 메서드가 없는 구현은 기존처럼
  즉시 격리해 protocol 하위 호환성을 유지한다.

### 완료 기준

- prompt 단위 테스트가 skeleton의 완전한 index 집합과 ellipsis 부재를 검증한다.
- pipeline 단위 테스트가 `invalid_json → retry success`, `schema_fail → retry
  success`, `retry final failure → quarantine`을 검증한다.
- 전체 pytest와 `git diff --check`가 통과한다.
- 동일 GCS raw 영상 20건으로 최소 5명의 실제 OpenRouter 호출을 수행해 각 응답의
  JSON 문법, row 수, index 집합, draft/event schema를 검토한다.

### 완료 결과

- 단위 테스트에서 complete skeleton, ellipsis 부재, invalid JSON 복구, schema
  불일치 복구, 재실패 quarantine을 검증했다.
- dev GCS raw 영상 20건 × virtual user 5명으로 OpenRouter Live QA를 수행했다.
- 최초 응답 5/5가 모두 유효 JSON, row 20개, row 길이 3, index `0..19`를
  만족했다. schema 교정 재시도 0회, quarantine 0건이었다.
- 판단값 100건은 `click_propensity` 10개 고유값(0.0~0.6),
  `watch_fraction` 10개 고유값(0.0~0.8)을 가져 skeleton placeholder를 단순
  복사하지 않았음을 확인했다.
- 최종 `action_log_ctr_v3` EventLog는 110행(impression 100, click 5, view 5),
  CTR 5%, `watch_time_sec` view-only 불변식을 만족했다.
- 후속 확대 QA에서 동일 raw 20건 × virtual user 100명을 동시성 2로 실행했다.
  최초 응답 100/100이 JSON/index 계약을 만족했고 schema retry와 quarantine은
  모두 0건이었다. 2,000개 judgment와 최종 EventLog 2,244행의 세션·스키마
  불변식도 전수 검증을 통과했다.

## PR #119 Claude 리뷰 후속 결정 (2026-07-12)

### 리뷰 판단

1. **메인 스레드의 동기 schema retry**: 지적을 수용한다. 현재 구현은 완료된
   future를 수집하는 coordinator에서 재시도 네트워크 호출을 실행하므로, 여러
   검증 실패가 한 번에 발생하면 재시도가 직렬화되고 완료된 worker 슬롯의
   재충전도 늦어진다. 정상 응답만 나온 100-user Live QA에서는 드러나지 않은
   failure-path 처리량 문제다.
2. **재시도 API 오류 상태 유실**: 지적을 수용하되 파싱 시간 계약을 명확히 한다.
   재시도 호출이 예외를 내면 최종 결과의 `error`와 `error_type=api_error`를
   보존한다. 단, 최초 응답은 실제로 도착해 검증됐으므로 그 최초 파싱 시간은
   0으로 버리지 않고 `parse_elapsed_ms`에 남긴다.
3. **재시도 파싱 시간의 request 귀속**: 지적을 수용한다. 재시도 timer가 두 번째
   `_try_build_user_drafts()`까지 감싸 request latency에 파싱 비용을 포함한 것은
   계측 계약 위반이다.

### worker lifecycle 계약

- 한 `(virtual user × candidate chunk)` worker가 최초 생성 요청, 최초 응답 검증,
  필요 시 1회 schema 교정 요청, 두 번째 응답 검증까지 모두 소유한다.
- coordinator는 완결된 work 결과만 수집하고 checkpoint·progress·결정론적 결과
  조립을 담당한다. coordinator에서는 LLM 네트워크 호출을 하지 않는다.
- schema retry도 기존 `max_concurrency` worker 슬롯 안에서 실행한다. 따라서 한
  work의 재시도 중 다른 worker가 끝나면 coordinator가 그 슬롯에 다음 work를
  제출할 수 있고, 여러 재시도는 worker 수 범위에서 병렬 진행된다.
- API 동시 호출 수는 최초 요청과 schema retry를 합쳐도 `max_concurrency`를 넘지
  않는다.

### 최종 오류와 raw 응답 계약

- 최초 생성 요청이 실패하면 `api_error`, 빈 raw 응답, `parse_elapsed_ms=0`으로
  격리한다. schema 교정 재시도는 하지 않는다.
- 최초 응답 검증 실패 후 schema retry 요청 자체가 실패하면 최종 `error`를
  보존하고 `api_error`로 격리한다. quarantine raw 응답은 진단 가능한 마지막
  수신 응답인 최초 실패 응답을 유지한다.
- schema retry 응답도 JSON/schema 검증에 실패하면 두 번째 raw 응답과 두 번째
  검증 오류 유형(`invalid_json` 또는 `schema_fail`)을 격리한다.
- 성공 draft와 최종 오류는 동시에 존재할 수 없다.
- worker에서 예상하지 못한 내부 예외는 `api_error`로 격리해 숨기지 않고 배치
  호출자에게 전파한다. generator 호출 경계에서 잡은 외부 API 예외만
  `api_error` 결과로 변환한다.

### 텔레메트리 시간 계약

- `request_elapsed_ms`: `generator.generate()`와
  `generator.generate_schema_retry()` 호출에 실제로 소비된 시간의 합. JSON 파싱,
  schema 검증, coordinator 처리 시간은 포함하지 않는다.
- `parse_elapsed_ms`: 최초 응답 및 재시도 응답에 대한
  `_try_build_user_drafts()` 실행 시간의 합. 네트워크 요청 시간은 포함하지 않는다.
- 재시도 API 오류가 나더라도 이미 수행된 최초 응답 파싱 시간은
  `parse_elapsed_ms`에 포함한다. 반대로 최초 API 오류처럼 파싱 자체가 없으면
  0이다.
- `queue_wait_ms`, checkpoint/progress/submit 계측과 최종 결정론적 조립 계약은
  변경하지 않는다.

### 추가 완료 기준

- 재시도가 block된 동안 다른 worker 완료 슬롯에 다음 work가 제출되는 동시성
  회귀 테스트가 통과한다.
- 재시도 API 오류 결과가 `error_type=api_error`와 실제 예외를 보존하고, 최초
  실패 raw 응답을 quarantine에 남기는 테스트가 통과한다.
- 제어된 clock으로 최초·재시도 request 시간의 합과 두 번의 parse 시간 합이
  서로 겹치지 않음을 검증한다.
