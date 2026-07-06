# Agent Simulator 

Event Log 생성 명세

## 목적

Persona Raw 데이터와 YouTube Video Metadata를 이용해 CTR 학습에 사용할 Event Log를 생성한다.

---

## 생성 단계 개요

Event Log는 두 단계(Phase 1, 2)로 나뉘어 생성된다. 스키마는 동일하며, `source` 값으로 단계를 구분한다.

| 단계 | source 값 | 시점 | 설명 |
|---|---|---|---|
| Phase 1 | `historical` | 추천 서버 실행 전 | Persona + Video 매칭으로 과거 로그를 시뮬레이션하며 데이터를 채운다 |
| Phase 2 | `online_simulated` | 추천 서버 실행 후 | Phase 1 방식을 중단하고, 에이전트가 추천 API를 직접 호출해 반환된 리스트에서 클릭을 시뮬레이션한다 |

두 단계는 순차적으로 전환되며, 추천 서버가 올라오는 시점을 기준으로 Phase 1에서 Phase 2로 자연스럽게 넘어간 후 적재되는 방식으로 진행한다.

---

## Event Log 정의

Event Log는 **long 포맷의 이벤트 스트림**이다: 한 row = 한 이벤트(`event_type` ∈
`impression`/`click`/`view`/`like`) 1건이며, 한 번의 노출(유저×영상)에 대해 여러
event_type 행이 시간순으로 누적될 수 있다. `clicked`/`liked` 같은 라벨·상태 컬럼은
별도로 두지 않으며, 사건이 실제로 일어난 순간에만 해당 `event_type` 행을 추가한다.

- `impression` 행 존재 = 노출됨. "노출 대상" 집합은 impression 행으로 정의한다.
- `click` 행 존재 = 클릭 발생(raw 사건). **라벨이 아니다** — `clicked` 라벨은 로그에
  없고, downstream 학습셋 빌더가 `impression LEFT JOIN click`으로 파생한다.
- `view` 행 존재 = 실제 시청(클릭 후 재생); `watch_time_sec`는 이 행에만 채워진다.
- `like` 행 존재 = 좋아요 발생.

PK는 `event_id`다. `(user_id, video_id)`는 **유니크하지 않은 FK**다 — 한 (유저, 영상)
쌍이 impression/click/view/like 최대 4행을 가질 수 있고, 같은 영상을 다른 시각에
다시 노출/시청할 수도 있다. 설계 근거:
[`docs/superpowers/specs/2026-07-06-event-log-long-format-design.md`](superpowers/specs/2026-07-06-event-log-long-format-design.md)

---

## 최종 스키마 (events 테이블)

| 컬럼 | 타입 | 설명 | 제약 |
|---|---|---|---|
| `event_id` | string | 이벤트 고유 ID | 고유값(PK) |
| `event_timestamp` | datetime(UTC) | 이벤트 발생 시각 | 아래 생성 규칙 참고 |
| `user_id` | string | Persona uuid 기반 사용자 ID | 필수, `virtual_users.user_id` FK(non-unique) |
| `event_type` | string | 이벤트 종류 | `impression` / `click` / `view` / `like` 중 하나 |
| `video_id` | string | 노출/시청된 YouTube 영상 ID | 필수, `videos.video_id` FK(non-unique) |
| `watch_time_sec` | int/null | 시청 시간(초) | `view`일 때만 non-null(>=0), 그 외 이벤트 타입은 null |
| `rank` | int/null | 추천 리스트 내 순위 | Phase 1은 항상 null(추천 서버 없음), Phase 2는 필수 |
| `source` | string | 데이터 출처 구분 | `historical` / `online_simulated` |

`(user_id, video_id)`는 PK가 아니라 **유니크하지 않은 FK**다. 같은 (유저, 영상) 쌍에
최대 4개 event_type 행이 존재할 수 있고, 미래엔 같은 영상을 다른 시각에 다시
노출/시청할 수도 있다.

**Phase 1(MVP)에는 없는 컬럼**: `clicked`, `liked`, `search_keyword`, `exposure_type`.
`clicked`/`liked`는 각각 `click`/`like` event_type 행으로 대체되어 컬럼 자체가
사라졌고, `search_keyword`·`exposure_type`·`session_id`·`request_id`·`query`·
`event_type=search`는 Phase 2(추천 서버) 도입 전까지 보류한다. 설계 근거:
[`docs/superpowers/specs/2026-07-06-event-log-long-format-design.md`](superpowers/specs/2026-07-06-event-log-long-format-design.md)

---

## Phase 1 생성 규칙 (historical)

### Persona → 관심사 추출

아래 컬럼을 이용해 유저 관심사를 추출한다.

- `hobbies_and_interests_list`, `hobbies_and_interests`
- `professional_persona`, `skills_and_expertise`
- `sports_persona`, `arts_persona`, `travel_persona`, `culinary_persona`, `family_persona`
- `persona` (전체 텍스트)

추출 방식은 구현자 재량으로 결정한다.

### Video → 주제 추출

아래 컬럼을 이용해 영상 주제를 추출한다.

- `title`, `description`, `tags`, `category_id`

### 매칭 기준

유저 관심사와 영상 주제가 가까울수록 LLM이 판단하는 `click_propensity`(클릭 확률)가
높아지도록 생성한다. 매칭 알고리즘은 구현자 재량으로 결정한다.

### 클릭 선정 및 이벤트 생성 규칙

`clicked` 컬럼은 없다. 노출(유저×영상) 1건당 이벤트는 다음 규칙으로 생성된다.

- **항상**: `impression` 1행 (`rank=null`, `watch_time_sec=null`, `source=historical`)
- **전역 상위 `round(target_ctr × N)`개로 "클릭" 선정된 impression에만 추가로**:
  - `click` 1행
  - `view` 1행 (`watch_time_sec = round(watch_fraction × duration_sec)`)
  - `like` 1행 — LLM `would_like=true`일 때만
- timestamp 순서: 같은 노출 흐름 내에서 `impression < click < view < like` (단조 증가)

클릭 여부 라벨(`clicked`)은 로그에 저장하지 않는다. downstream 학습셋 빌더가
`impression LEFT JOIN click`으로 파생한다 — impression만 있고 click 행이 없으면
"노출됐지만 클릭 안 함"을 자연스럽게 의미하므로, 별도의 `clicked=0` 제약(watch/like
강제 0)은 불필요하다. 설계 근거:
[`docs/superpowers/specs/2026-07-06-event-log-long-format-design.md`](superpowers/specs/2026-07-06-event-log-long-format-design.md)

### 라벨 비율

전체 impression 중 "클릭"으로 선정되는 비율(전역 CTR = click 행 수 / impression
행 수)은 **약 2% 내외**를 목표로 한다. (멘토 지정) 코드가 LLM `click_propensity`
상위 `round(target_ctr × N)`개를 선정해 정확한 비율을 보장한다(LLM이 비율을 정하지
않는다).

### timestamp 생성 규칙

`recent_click_count`, `recent_watch_time_7d` 등 Historical User Feature를 집계할 때 timestamp가 현실적이지 않으면 피처 자체가 의미 없어진다. 아래 기준을 지킨다.

- 전체 로그는 범위 안에 분산되도록 생성한다
- 유저별 하루 노출(impression) 수는 현실적인 범위 내로 제한한다. `max_events_per_user_per_day`는
  **impression 기준** 상한이며, 같은 노출에서 파생된 click/view/like는 상한 계산에
  별도로 포함하지 않고 해당 impression 시각 직후에 배치한다.
- 동일 유저의 이벤트가 특정 날짜에 과도하게 몰리지 않도록 한다

---

## Phase 2 생성 규칙 (online_simulated)

> **주의**: 아래 `exposure_type`, `session_id`, `request_id`, `query`(검색어),
> `event_type=search`는 모두 **Phase 2 도입 시점에 결정/추가**되는 보류 항목이며,
> 현재 구현된 Phase 1 events 스키마(`autoresearch/action_logs/schema.py`)에는
> 존재하지 않는다. `rank`만 Phase 1 스키마에 이미 있으나 항상 `null`이다.

추천 서버가 실행되면 Phase 1 방식을 중단하고, 에이전트가 **한 시간에 한 번씩 추천 API를 직접 호출**해 반환된 리스트에서 클릭을 시뮬레이션하여 저장한다.

### 추천 리스트 구성

API가 반환한 Top-N 리스트를 기준으로 아래와 같이 구성한다.

- 대부분은 모델 점수 또는 인기도 기준 상위 아이템 → `top_ranked`
- 일부는 다양성 확보를 위해 랜덤으로 삽입한 아이템 → `exploration`

비율은 구현 시점에 결정하며, 예시로 Top 10 중 8개 `top_ranked` + 2개 `exploration` 정도면 충분하다.

### rank, exposure_type

API 응답을 기반으로 아래 값을 채운다.

- `rank`: 추천 리스트 내 순위 (1부터 시작)
- `exposure_type`: `top_ranked` 또는 `exploration`
- 시뮬레이터는 Phase 1과 동일하게 내부적으로 Persona-Video 매칭 점수를 사용해 클릭 확률을 계산한다. exposure_type은 시뮬레이터의 점수 사용 여부가 아니라, 서버가 해당 영상을 어떤 전략으로 노출했는지를 나타낸다.

### 클릭 시뮬레이션

Phase 1과 동일한 방식으로 Persona 관심사와 영상 주제를 매칭해 클릭 여부를 시뮬레이션한다. 라벨 비율 목표(약 2%)도 동일하게 유지한다.

---

## 제약 요약

| 항목 | 요구사항 |
|---|---|
| 라벨(`clicked`) | 로그에 없음. `impression LEFT JOIN click`으로 downstream에서 파생 |
| `watch_time_sec` | `view` 이벤트만 non-null(>=0), 그 외 event_type은 null 강제 |
| 라벨 비율(전역 CTR) | click 행 수 / impression 행 수 ≈ 2% 내외 |
| timestamp | 범위 내 현실적 분산, 같은 노출 흐름은 `impression<click<view<like` (Phase 1 기준) |
| 일일 상한(`max_events_per_user_per_day`) | impression 기준으로 적용, 파생 click/view/like는 상한에 별도 계산하지 않음 |
| `rank` | Phase 1은 항상 null, Phase 2에서 필수 |
| `exposure_type`, `session_id`, `request_id`, `query`, `event_type=search` | Phase 1 스키마에 없음 (Phase 2 보류) |
