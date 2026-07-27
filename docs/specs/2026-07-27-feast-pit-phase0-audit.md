# Feast PIT 전환 Phase 0 감사 — offline store 실적재 실측

- Epic: [EPIC] #299 학습 데이터셋 Feast PIT 조회 전환
- Issue: #356 (Phase 0)
- 측정일: 2026-07-27 (KST)
- 측정 도구: `scripts/verify_offline_coverage.py`, `scripts/cleanup_dummy_seed.py`
- 대상: `ar-infra-501607.feast_offline_store`
- 윈도우: 최근 30일(어제까지), KST 날짜 기준

## 목적

PIT(as-of) 조회가 성립하려면 **학습 윈도우 전 기간의 스냅샷이 실제로 적재돼
있어야 한다.** 이 문서는 그 재료가 갖춰졌는지를 실측으로 판정하고, Phase 0 완료
여부와 후속 조치를 확정한다.

## 판정: ❌ 재료 미충족 — 백필·빌드 선행 필요

실측 결과 PIT 조회의 spine(`training_entity`)이 미빌드이고, 일일 동적 피처
2종에 큰 결손이 있어 **현 상태로는 Phase 1(#357) 조회 전환을 시작할 수 없다.**

> **2026-07-27 갱신**: infra#362로 `training_entity` 테이블 생성 후 3종 모두 백필 완료.
> **원천 데이터 있는 날은 결손 0** — feature-store 관점 Phase 0 완료다. 남은 결손은
> 전부 원천(action_log/트렌딩) 수집 구멍이며 **daily 폐루프 신뢰성(upstream, 별도
> 이슈)** 소관이다. Phase 1(#357)은 정상 11일로 착수 가능. 상세는 아래 "갱신" 절.

## 실측 결과

| 테이블 | 상태 | 커버리지(윈도우 30일) | 비고 |
| --- | --- | --- | --- |
| `training_entity` | ❌ 미빌드 | 조회 불가(테이블 없음 추정) | #245/#355는 코드만, 프로덕션 실행 안 됨 |
| `user_dynamic_feature` | ⚠️ 결손 | **8/30일** (22일 결손), 윈도우 내 55,864 row | 최근 며칠만 존재 |
| `video_feature` | ⚠️ 결손 | **5/30일** (25일 결손), 윈도우 내 997 row | 가장 희소 |
| `user_static_feature` | ✅ 적재됨 | real 6,983 row | 정적 — sentinel ts(1970-01-01) 정상 |
| `user_category_similarity` | ✅ 적재됨 | real 104,745 row | 정적 — sentinel ts(1970-01-01) 정상 |

- **더미 seed**: 4개 테이블 모두 `user_`/`video_` 접두 더미 **0 row** — 오염 없음.
  (실측이 seed로 왜곡되지 않았음을 확인.)
- 정적 테이블 2종의 `1970-01-01` 최신 시각은 "날짜 개념 없는 정적 피처"의 sentinel
  타임스탬프로, PIT 조회가 항상 이 행을 고르게 하는 정상 설계다.

## 갱신 — 2026-07-27 백필 후

**보정 1: 데이터 시작일 = 2026-07-07.** 위 30일 창(06-27~)은 데이터 수집 시작 전
10일을 헛되이 "결손"으로 셌다(원천 `data_lake_youtube_trending_kr`가 07-07부터).
커버리지·백필은 **데이터 있는 창** 기준으로 봐야 한다 — `--days`로 창을 맞춰 재측정한다.

**백필 결과 (`scripts/backfill_feature_store.py --apply`, 창 07-07~07-26):**

| 테이블 | 백필 후 | 남은 결손 |
| --- | --- | --- |
| `user_dynamic_feature` | **20/20 결손 0** ✅ | 없음 (활동 없는 날도 유저 폴백으로 0-스냅샷) |
| `video_feature` | **18/20** | 07-08, 07-11 (트렌딩 수집 구멍) |
| `training_entity` | **13/20** (infra#362 테이블 생성 후 백필) | 07-08·09·10·11·22·25(action_log 없음) + 07-26(D+2 미도래) |

- 위 결손은 전부 **원천 데이터 자체가 없는 날**이라 백필로 못 채운다(feature-store 문제
  아님). `feature_store_build`의 "0행 → 검증 fail-closed"로 안전 실패(빈 파티션 DELETE는
  no-op). dry-run은 실행 안 해 못 잡고 실 `--apply`에서만 드러난다.
- `video`/`training_entity` 결손은 PIT에 치명적이지 않다: video는 ASOF로 직전 스냅샷이
  붙고, action_log 없는 날은 impression(=학습 예제) 자체가 없어 PIT 조회 대상이 아니다.

**발견 — daily 폐루프 신뢰성 (upstream, 별도 이슈).** action_log impression 수/일
(07-07~07-25 실측): 정상 풀데이(~16만) **11일**(07-07, 12~21) · **완전 없음 6일**
(07-08,09,10,11,22,25) · **거의 빔 2일**(07-23·07-24 = **240개**, 정상의 0.15%). 즉
19일 중 온전한 날이 절반 남짓이다. feature-store는 원천을 정확히 반영했을 뿐,
근본 원인은 폐루프(action_log 생성)가 그 기간에 안정적으로 돌지 않은 것 —
`Autoresearch-airflow` 신뢰성 이슈로 분리한다.

## ttl 부재 → 결손일 stale fallback (실증)

FeatureView 4종 모두 `ttl`이 없어, 결손일 조회가 `null`이 아니라 **더 오래된
스냅샷(stale)**을 조용히 붙인다. 위 결손(22·25일)과 결합되면 학습이 "그 시점 값"이
아니라 "며칠 전 값"을 배우는 왜곡이 된다.

- 실증: `tests/test_offline_retrieval_smoke_feast.py` — 07-02 결손 상태에서 07-02
  조회 시 07-01 스냅샷이 붙음을 단언(feast 그룹 CI pass).
- 상세: `docs/guides/feature-store.md` "ttl 부재 → 결손일 stale fallback 위험".

## Phase 0 완료 조건 대비

| #356 완료 조건 | 상태 |
| --- | --- |
| TEMP_FEAST_BOOTSTRAP seed 잔재 0 | ✅ 실측 0 row |
| offline 조회 스모크 CI green | ✅ feast 그룹 2 pass + `ci.yml` 등록 |
| 커버리지 문서화 | ✅ 본 문서 |
| **결손일 0 (또는 백필 완료)** | ✅ 3종 모두 **원천 있는 날 결손 0**으로 백필 완료(training_entity는 infra#362 테이블 생성 후). 남은 결손은 원천 수집 구멍 = upstream 폐루프 신뢰성(별도 이슈) |

## 필요 조치 (백필·빌드 — `Autoresearch-airflow`/ops)

Phase 0을 완료(=Phase 1 착수 가능)로 넘기려면:

1. **`training_entity` 빌드** — `python -m autoresearch.jobs.feature_store_build
   --tables training_entity --partition-date <D>`를 윈도우 날짜별로 백필. spine이
   없으면 PIT 조회 대상 자체가 없다. (테이블 스키마는 `Autoresearch-infra` 소유 —
   테이블 존재부터 확인.)
2. **`user_dynamic_feature`·`video_feature` 결손일 백필** — 같은 CLI를 결손 날짜별로
   반복 실행(멱등).
3. 백필 후 **`verify_offline_coverage.py` 재실행 → 결손 0 확인** → 본 문서 갱신.

## 완료 선언 기준

위 백필로 **재측정 결손 0**이 되면 Phase 0을 완료로 선언하고, **Phase 1(#357)
DuckDB ↔ offline store 피처 계약 정렬**에 착수한다. `ttl` 정책은 #357에서 결정한다
(결손을 `null`로 차단할지, 백필로만 메울지 포함).

> 요약: **측정은 완료, 결과는 미충족.** 코드 재료(spine 빌드 CLI·스모크)는 갖춰졌고,
> 남은 건 실데이터 백필(ops)이다.
