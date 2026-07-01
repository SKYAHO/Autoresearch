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

한 row는 **특정 사용자에게 특정 영상이 노출된 사건 1회**를 의미한다.

노출되지 않은 영상은 row를 생성하지 않는다. `impression` 컬럼은 별도로 두지 않으며, row가 존재한다는 것 자체가 impression=1을 의미한다.

---

## 최종 스키마 (events 테이블)

| 컬럼 | 타입 | 설명 | 제약 |
|---|---|---|---|
| `event_id` | string | 이벤트 고유 ID | 고유값 |
| `event_timestamp` | datetime | 노출 발생 시각 | 아래 생성 규칙 참고 |
| `user_id` | string | Persona uuid 기반 사용자 ID | 필수 |
| `video_id` | string | 노출된 YouTube 영상 ID | 필수 |
| `clicked` | int | 클릭 여부 (Label) | 0 또는 1 |
| `watch_time_sec` | int | 시청 시간(초) | clicked=0이면 반드시 0 |
| `liked` | int | 좋아요 여부 | clicked=0이면 반드시 0 |
| `search_keyword` | string/null | 유저 관심 키워드 | optional |
| `source` | string | 데이터 출처 구분 | `historical` / `online_simulated` |
| `rank` | int/null | 추천 리스트 내 순위 | Phase 1은 null, Phase 2는 필수 |
| `exposure_type` | string/null | 노출 전략 구분 | 추천 서버의 노출 전략을 기록하는 컬럼. Phase 1은 서버 없이 시뮬레이터가 직접 노출를 결정하므로 null. Phase 2는 서버가 반환한 전략에 따라 `top_ranked`(모델 점수 상위) / `exploration`(다양성 목적 랜덤 삽입)으로 기록. |

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

유저 관심사와 영상 주제가 가까울수록 `clicked=1` 확률이 높아지도록 생성한다. 매칭 알고리즘은 구현자 재량으로 결정한다.

### 라벨 비율

전체 row 중 `clicked=1` 비율은 **약 2% 내외**를 목표로 한다. (멘토 지정)

### timestamp 생성 규칙

`recent_click_count`, `recent_watch_time_7d` 등 Historical User Feature를 집계할 때 timestamp가 현실적이지 않으면 피처 자체가 의미 없어진다. 아래 기준을 지킨다.

- 전체 로그는 범위 안에 분산되도록 생성한다
- 유저별 하루 이벤트 수는 현실적인 범위 내로 제한한다
- 동일 유저의 이벤트가 특정 날짜에 과도하게 몰리지 않도록 한다

---

## Phase 2 생성 규칙 (online_simulated)

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
| clicked=0일 때 | `watch_time_sec=0`, `liked=0` 강제 |
| 라벨 비율 | clicked=1 약 2% 내외 |
| timestamp | 범위 내 현실적 분산 (Phase 1 기준) |
| rank, exposure_type | Phase 1은 null, Phase 2에서 필수 |
