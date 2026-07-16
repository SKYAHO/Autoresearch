# Event Log 재설계: wide → long 이벤트 스트림 (Phase 1 MVP)

- 일자: 2026-07-06
- 이슈: #57
- 브랜치: `feat/57-event-log-long-format` (현재 HEAD `75c1c3d` 기준, refactor/51 위에 스택)
- 근거 문서: 팀 Notion "event log" 설계 (raw event log ≠ CTR training dataset)

## 1. 배경 / 문제

기존 구현(커밋 `75c1c3d`)의 `EventLog`는 **wide 포맷**이다: 한 row = 한 impression이고
`clicked`, `watch_time_sec`, `liked`, `search_keyword`, `exposure_type`를 그 row에 눌러담았다.

팀 Notion "event log" 설계는 정반대를 못박는다:

> "클릭여부가 event log에 있으면 안 되고, Training dataset에 있어야 한다."
> "Raw Event Log는 모델 입력이 아니라, clicked Label과 User Behavior Feature를 만들기 위한 원천 로그."

즉 **저장 목적은 feature engineering용 train set을 뽑기 위한 원천 로그**다. `clicked`,
`recent_click_count`, 야간시청비율 같은 라벨·피처는 로그에 미리 박는 게 아니라 나중에
`GROUP BY`/join으로 파생해야 자유롭게 새 피처를 만들 수 있다. 로그에 라벨을 박으면
(1) 새 피처를 못 만들고 (2) label leakage가 구조적으로 발생한다.

이 설계는 `EventLog`를 **long 이벤트 스트림**으로 전환한다. 결과적으로 이전에 발견한
개선 2건(search_keyword leakage, exposure_type SSOT)은 자동 소멸한다.

## 2. 역할 분담 (경계)

```
[이 작업]  user action logs = 원본 이벤트 로그 (impression/click/view/like)
              │
              ▼
[다음 레이어, 범위 밖]  feature store / 학습셋 담당
   - impression ↔ click join → clicked 라벨
   - view/like/click 집계 → dynamic feature (recent_click_count, 세션길이 …)
   - persona static + video feature + interaction feature 결합 → CTR training dataset
```

이 작업의 산출물은 라벨도 피처도 아닌, 그것들을 파생할 수 있는 **원본 이벤트 로그**다.

## 3. events 스키마 (MVP 필수 8컬럼)

| 컬럼 | 타입 | 규칙 |
|---|---|---|
| `event_id` | string | 고유값 |
| `event_timestamp` | datetime(UTC) | 이벤트 발생 시각 |
| `user_id` | string | 사용자 |
| `event_type` | string | `impression` / `click` / `view` / `like` (단일 컬럼, 값 4종) |
| `video_id` | string | 영상 이벤트라 항상 채움 (search 없으니 null 미사용) |
| `watch_time_sec` | int/null | **`view`일 때만** 값, 나머지 null |
| `rank` | int/null | **Phase 1은 항상 null** (추천 서버 없음 → 개인화 순위 개념 자체가 없음) |
| `source` | string | `historical` (Phase 1 고정) |

**현행에서 제거**: `clicked`(→ `click` 이벤트), `liked`(→ `like` 이벤트),
`search_keyword`·`exposure_type`(→ Phase 2 / optional로 보류).

**event_type = `view`** (네이밍: 이벤트 타입은 `view`, 시청시간은 `watch_time_sec` 컬럼).

### 보류(Phase 2 / optional, MVP 제외)
`session_id`, `request_id`, `exposure_type`(top_ranked/exploration), `query`(search),
그리고 `event_type=search`. Phase 2(추천 서버) 도입 시 `rank`·`exposure_type`과 함께 추가.

### 이벤트 semantic / 테이블 관계 (확정)
- **PK**: `events` 테이블의 진짜 PK는 `event_id`다. `(user_id, video_id)`는 **유니크하지 않은 FK**다
  — 한 (유저, 영상) 쌍이 `impression`/`click`/`view`/`like` 최대 4행을 가질 수 있고, 미래엔
  같은 영상을 다른 시각에 다시 노출/시청할 수도 있다. `videos`(PK=`video_id`)·`virtual_users`
  (PK=`user_id`)를 참조하는 자식 이벤트 로그다.
- **이벤트별 의미**:
  - `impression` 행 존재 = **노출됨**(추천/결과에 떴다). "노출 대상" 집합은 impression으로 정의.
  - `click` 행 존재 = **클릭 발생**. 이는 라벨이 아니라 raw 사건이며, `clicked` 라벨은
    downstream에서 `impression LEFT JOIN click`으로 파생한다(로그에 `clicked` 컬럼 없음).
  - `view` 행 존재 = **실제 시청**(클릭 후 재생). `watch_time_sec`는 이 행에만 붙는다.
    "영상을 본 사람" = 해당 `video_id`에 `view` 행이 있는 유저로 정의한다.
  - `like` 행 존재 = 좋아요 발생.
- **왜 `clicked=0/1` 컬럼이 아니라 `click` 이벤트인가**: wide의 `clicked` boolean은 모든 impression
  행에 라벨을 미리 박아 leakage를 유발했다. long은 클릭이 실제 일어난 순간에만 `click` 행 1개를
  추가하므로, 이벤트 로그 자체엔 라벨이 없고(=원천), 라벨은 학습셋 빌더가 join으로 만든다.

## 4. 이벤트 생성 규칙 (유저 × 노출영상 1건당)

- **항상**: `impression` 1행 (rank=null, watch_time=null, source=historical)
- **"클릭"으로 선정된 impression만 추가로**:
  - `click` 1행
  - `view` 1행 (`watch_time_sec` = `round(watch_fraction × duration_sec)`)
  - `like` 1행 — LLM `would_like=true`일 때만
- timestamp 순서: `impression < click < view < like` (같은 세션 흐름 내 단조 증가)

`clicked=0`이라는 표현은 존재하지 않는다. impression만 있고 click 행이 없으면 곧
"노출됐지만 클릭 안 함"을 의미한다(자연 표현). 따라서 기존 `enforce_no_click_constraints`
(clicked=0 ⇒ watch/like=0) 제약은 **불필요**해진다.

**일일 상한 의미**: `max_events_per_user_per_day`는 **impression(노출) 기준**으로 적용한다.
한 impression에서 파생된 click/view/like는 같은 세션의 후속 이벤트이므로 상한에
별도로 계산하지 않고, 해당 impression 시각 직후에 배치한다.

## 5. 전역 2% CTR 정규화 (로직 유지, 출력만 변경)

1. LLM이 (유저×후보영상)마다 `click_propensity`, `watch_fraction`, `would_like` 판단.
2. 코드가 전체 impression 중 propensity 상위 `round(target_ctr × N)`개를 "클릭"으로 선정.
3. 선정된 impression에만 click/view/like 행을 추가로 **확장(expand)**.

기존과 동일하게 **코드가 정확한 클릭 비율을 결정**(LLM이 비율을 정하지 않음).
차이는 산출물뿐: 이전엔 `clicked=1`을 찍었고, 이제는 이벤트 행을 추가한다.

## 6. LLM 역할 (거의 동일)

- LLM 판단 필드: `click_propensity`(0~1), `watch_fraction`(0~1), `would_like`(bool).
  - `search_keyword`는 MVP에서 불필요 → 프롬프트/드래프트에서 제거.
- 코드 조립: 후보 구성 → 이벤트 확장 → timestamp 분산 → 2% 정규화 → parquet/jsonl.
- 유저 단위 장애 격리 + quarantine(api_error/invalid_json/schema_fail) + 전량실패 가드
  (`max_quarantine_ratio`)는 그대로 유지.

## 7. 생성 흐름

```
virtual_users(10) + KR TrendingVideo(200)
   → candidate.build_candidates (유저당 24 후보; 관련 + exploration)   # 내부 생성용, 로그엔 exposure_type 안 남김
   → LLM 판정 (propensity/watch_fraction/would_like)
   → 전역 상위 2% impression을 "클릭"으로 선정
   → 이벤트 확장: 노출 24개 → impression 24행 (+클릭분 click/view/like)
   → EventLog(long) 검증
   → parquet(EVENT_LOG_PARQUET_SCHEMA) + warehouse jsonl + quarantine jsonl
```

## 8. 구현 변경 사항 (autoresearch/action_logs/)

- **schema.py**
  - `EventLog`: `event_type: Literal["impression","click","view","like"]` 추가;
    `video_id: str`; `watch_time_sec: int | None`; `rank: int | None`(항상 null);
    `clicked`/`liked`/`search_keyword`/`exposure_type` **제거**.
  - 검증기: `watch_time_sec`는 `view`일 때만 non-null(그 외 null), 그 외 이벤트는 null 강제.
  - `to_warehouse_row`에서 제거 컬럼 반영.
  - `enforce_no_click_constraints` **삭제**.
  - `ImpressionDraft`: `search_keyword`·`exposure_type` 제거(would_like 유지).
- **candidate.py**: exposure_type 라벨을 로그에 안 남기므로 반환 형태 단순화(관련/랜덤 구분은
  내부 후보 구성에만 사용). 시그니처 조정.
- **llm_generator.py**: 프롬프트 출력 스펙에서 `search_keyword` 제거. `would_like` 유지.
  RuleBased/OpenRouter 동일.
- **pipeline.py**:
  - `_assemble_events` → **`_expand_events`**: 유저별 draft + 클릭선정 결과를 받아
    impression 행 + (선정분) click/view/like 행으로 확장, timestamp 단조 배치.
  - `EVENT_LOG_PARQUET_SCHEMA`를 long 스키마로 갱신.
  - `summary`(CTR 계산): 이제 `impression 행 수`와 `click 행 수`로 계산
    (`clicks / impressions`).

## 9. SSOT 문서 갱신

`docs/guides/agent-simulator-spec.md`를 이 long 설계로 갱신한다(현재 wide + clicked 컬럼 서술이
코드와 충돌하므로 필수). event_type 도입, clicked/liked 컬럼 서술 제거,
"라벨은 학습셋에서 파생" 명시, Phase 1 rank=null 유지.

## 10. 테스트 (`tests/test_action_logs_pipeline.py` 재작성)

- impression은 노출영상마다 1행 존재.
- "클릭 선정"된 impression에만 click/view/like 동반(그 외엔 impression만).
- 전역 CTR ≈ target(= click 행 수 / impression 행 수) — round(0.02×N).
- `view` 행만 watch_time_sec > 0, 나머지 이벤트는 null.
- 모든 행 rank=null(Phase 1), source=historical.
- timestamp: 유저별 이벤트가 history window 내, 같은 클릭묶음은 impression<click<view<like.
- 유저 단위 격리·전량실패 가드 동작.
- parquet 스키마 일치.

## 11. QA 재생성

`mistral-nemo`로 vu 10명 × KR 200영상 재실행 → long 이벤트 로그 생성 후 리포트 갱신
(`autoresearch/action_logs/docs/`). 확인 지표: impression 총수, click 행수/CTR≈2%,
view watch_time 분포, 격리 0, 스키마 검증 통과.

## 12. 범위 밖 (명시)

- CTR training dataset 빌더(impression↔click join, dynamic feature 집계) — 다음 레이어.
- Phase 2(online_simulated, 추천 서버, rank/exposure_type 실제값).
- `search` 이벤트 및 검색 기반 피처.
- 100k 스케일·병렬 생성.
