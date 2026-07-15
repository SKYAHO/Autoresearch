# virtual_user_action_log 생성 파이프라인 설계 (Phase 1 / MVP)

- 이슈: #57
- 날짜: 2026-07-06
- 상태: 브레인스토밍 확정 → 구현
- SSOT: `docs/guides/agent-simulator-spec.md` (event 스키마·노출/라벨 규칙). 본 문서는 이를 **재정의하지 않고** Phase 1을 LLM 매칭으로 구현하는 방법만 다룬다.

## 1. 목적 / 범위

`virtual_users`(PR #55) 다음 단계. **YouTube backfill(`TrendingVideo`)** 과 **`VirtualUser`** 를 근거로 CTR 학습용 **event log(action log)** 를 생성한다.

이번 MVP 범위:
- **Phase 1(historical)만.** 추천 서버가 필요한 Phase 2(online_simulated)는 이후 이슈.
- **작은 규모**: 기존 생성된 virtual_users 소수(예: 10명) × KR TrendingVideo ~200건 pool로 end-to-end 동작 + 스키마 준수 + 전역 `clicked≈2%` 검증.
- 순차 격리 루프(virtual_users 패턴 재사용). 병렬화/100k는 범위 밖.

## 2. 입력

### 2.1 VirtualUser (기존 생성물, warehouse jsonl)
사용 필드: `user_id`(=virtual_user_id), `category_affinity`(map<cat,float>), `primary_categories`, `watch_time_band`, `persona_summary`, `interest_keywords`/`hobby_keywords`, `age`, `sex`.

### 2.2 TrendingVideo pool (KR backfill 샘플 ~200)
Kaggle `asaniczka/trending-youtube-videos-113-countries`에서 `country='KR'` 필터로 추출.
사용 필드: `video_id`, `title`, `description`, `video_tags`, `view_count`, `like_count`, `comment_count`, `publish_date`/`snapshot_date`, `channel_name`.
- **카테고리 컬럼 부재 대응**: 이 데이터셋엔 YouTube 카테고리 컬럼이 없다. 후보 선택(candidate)의 관련도는 **유저 관심 키워드/primary_categories ↔ 영상 title/tags 토큰 겹침** 휴리스틱으로 계산한다(코드). 클릭 판단 자체는 LLM이 실제 title/description을 읽어 수행하므로 카테고리 컬럼 없이도 성립한다.

## 3. 출력 스키마 (events, SSOT 준수)

| 컬럼 | 타입 | 생성 주체 | 규칙 |
|---|---|---|---|
| `event_id` | str | 코드 | 고유(예: `evt_{user}_{video}_{seq}`) |
| `event_timestamp` | datetime | 코드 | historical window 내 현실적 분산 |
| `user_id` | str | 코드 | VirtualUser |
| `video_id` | str | 코드 | 노출 영상 |
| `clicked` | int(0/1) | 코드(LLM propensity → 전역 2% 임계) | Label |
| `watch_time_sec` | int | 코드(LLM watch_fraction × duration) | clicked=0 ⇒ 0 |
| `liked` | int(0/1) | 코드(LLM would_like) | clicked=0 ⇒ 0 |
| `search_keyword` | str/null | LLM | optional |
| `source` | str | 코드 | `historical` 고정 |
| `rank` | int/null | 코드 | Phase 1 = null |
| `exposure_type` | str/null | 코드 | 후보 출처: `top_ranked`(관련) / `exploration`(랜덤) |

## 4. 역할 분담 (확정: LLM 판단 + 코드 조립)

- **LLM**: 유저 1명 × 후보 영상 batch를 실제 `title`/`description` 근거로 읽고, 후보별로
  `click_propensity(0~1)`, `watch_fraction(0~1)`, `would_like(bool)`, `search_keyword(str|null)`를 판단.
- **코드**: impression row 전개, **전역 2% CTR 정규화**, `event_timestamp` 분산, `clicked=0 ⇒ watch/like=0` 강제, `event_id`/`source`/`rank`/`exposure_type` stamp.

### LLM 출력 계약 (유저당 1콜)
```json
{ "judgments": [
  {"video_id":"...", "click_propensity":0.83, "watch_fraction":0.6,
   "would_like":true, "search_keyword":"LCK 하이라이트"},
  ...
]}
```
> LLM은 하드 0/1이 아니라 **propensity**를 준다 → 코드가 전역 분포에 임계치를 걸어 **정확히 ~2%만 clicked=1**.

## 5. 노출(candidate) 구성 — Z 하이브리드

유저별 노출 batch(M개, 기본 M≈24):
- **관련 후보**(≈80%): 유저 primary_categories/interest_keywords ↔ 영상 title/tags 토큰 겹침 점수 상위 → `exposure_type="top_ranked"` 씨앗.
- **exploration**(≈20%): pool에서 랜덤 → `exposure_type="exploration"` 씨앗.
- seed 고정(재현성). video는 `video_id`로 dedup.

## 6. 코드 조립 알고리즘

1. 전 유저의 모든 `(user, video, click_propensity)` 수집.
2. **전역 2% 정규화**: propensity 내림차순 정렬 → 상위 `round(0.02 × 총노출수)` 개를 `clicked=1`, 나머지 `clicked=0`. (SSOT "전체 row 중 clicked=1 ≈2%".)
3. clicked=1 행:
   - `watch_time_sec = round(watch_fraction × duration_sec)`. duration 없으면 `video_id` seed로 60~900s 명목값 샘플.
   - `liked = 1 if would_like else 0`.
   - `search_keyword` = LLM 값(있으면).
4. clicked=0 행: `watch_time_sec=0`, `liked=0`.
5. **timestamp**: historical window(기본 최근 30일) 내 분산. 유저별 하루 이벤트 수 상한(기본 ≤8/일)으로 특정 날짜 몰림 방지. seed 고정.
6. stamp: `event_id`, `source="historical"`, `rank=null`, `exposure_type`(후보 출처).

## 7. 아키텍처 / 모듈 (`autoresearch/action_logs/`)

| 모듈 | 역할 |
|---|---|
| `schema.py` | `EventLog`(row), `EventGenerationRequest`, `QuarantineRecord`, `EventGenerationResult`, 버전 상수 |
| `video_source.py` | KR TrendingVideo 샘플 로드(parquet/jsonl) → `VideoRecord` dict, `video_id` dedup, fixture 생성기 |
| `candidate.py` | 유저별 Z 하이브리드 후보 batch 구성(관련+exploration), seed |
| `llm_generator.py` | system harness + prompt(유저 profile + 후보 batch) → judgments 반환. RuleBased fixture + OpenRouter generator |
| `pipeline.py` | 유저 단위 격리 생성 → 전역 2% 정규화 → 조립 → parquet/warehouse/quarantine 저장 + 전량실패 가드 |

## 8. 장애 격리 / quarantine

- **유저 단위 격리**: 한 유저의 LLM 실패(api_error/invalid_json/schema_fail)가 배치를 죽이지 않음 → `quarantine.jsonl`.
- 예외 순서(load-bearing): `json.JSONDecodeError` → `(ValidationError, ValueError, KeyError, TypeError, AttributeError)`.
- **전량/대량 실패 가드**: `max_quarantine_ratio`(기본 0.5) 초과 시 격리 파일 남기고 `ActionLogGenerationError` raise (virtual_users 패턴 재사용).

## 9. 출력

- parquet(명시적 Arrow schema) — `asset/action_log/…` 또는 `data/generated/…`.
- warehouse jsonl (flat row) + quarantine jsonl.
- **QA 리포트**: 총 event 수, 전역 CTR, clicked=0 제약 위반 0 확인, timestamp 범위, exposure_type 분포, 유저별 노출 수.

## 10. 테스트

- `RuleBasedActionLogGenerator`(fixture)로 결정론적 pytest:
  - clicked=0 ⇒ watch_time_sec=0·liked=0 강제.
  - 전역 2% 정규화(총노출 대비 clicked 비율).
  - timestamp가 window 내·유저별 일 상한.
  - 유저 단위 격리 → quarantine, 전량 실패 → raise.
- ruff 통과, 기존 스위트 무회귀.

## 11. 이후(범위 밖)

- Phase 2(online_simulated): 추천 API 연동, `rank`/`exposure_type` 실제화.
- 100k 스케일: 병렬/Batch API.
- 실제 backfill 파이프라인(transform 스키마 정합) 정식 적재.
