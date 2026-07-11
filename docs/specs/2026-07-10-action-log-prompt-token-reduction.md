# Spec: Action Log 프롬프트 토큰 절감 (포맷 최적화)

- Status: Draft
- Date: 2026-07-10
- Owner: Airflow Orchestration (bbungjun)
- 관련 모듈: `autoresearch/action_logs/` (`llm_generator.py`, `pipeline.py`, `schema.py`)
- 관련 문서: `docs/AGENT_SIMULATOR_SPEC.md` (출력 스키마 SSOT),
  `autoresearch/action_logs/README_action_log.md`

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
