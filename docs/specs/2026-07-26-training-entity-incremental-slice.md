# training_entity 증분 슬라이스 적재 계약

- Status: Draft
- Issue: #245 (training_entity를 `feature_store_build.py`로 이관)
- 관련: #299(Feast PIT 전환 엄브렐러) Phase 0 선행, #295(action log 당일 슬라이스 A안),
  #261(feature_store_build 증분 적재), 폐기 PR #246(선행 시도, 전량 재구축 방식)
- 정본 참조: `docs/guides/data-warehouse.md`의 `training_entity` 절

## 배경

`training_entity`는 학습 데이터셋의 **spine** — "어느 (user, video, event_timestamp)
조합을 학습 대상으로 삼고, 그 impression이 클릭됐는지(label)"를 정의하는 기준
테이블이다. Feast Historical Retrieval이 이 테이블을 entity dataframe으로 받아
User/Video/Similarity 피처를 PIT join한다. 즉 **#299 PIT 전환의 조회 대상 자체**가
이 테이블이며, 이게 없으면 Phase 0 실측도 성립하지 않는다.

현재 `training_entity`는 저장소 어디서도 빌드되지 않는다. 원본 SQL이 있던
`src/pipeline/build_feature_tables.py`는 삭제됐고(#245 체크리스트 중 "삭제"만
반영됨), `autoresearch/jobs/feature_store_build.py`로의 이관(#246)은 머지되지
못했다. 그 사이 `feature_store_build.py`는 #262(증분 DELETE+INSERT) / #295(당일
슬라이스) 계약으로 재구조화됐으므로, 전량 재구축(`CREATE OR REPLACE`)이던 #246의
SQL은 **파티션 모델부터 재설계**해야 한다.

## 컬럼 계약

`data_lake_raw.data_lake_action_log`의 `impression`을 기준 row, `click`을 positive로
삼는다. 컬럼은 정본(`data-warehouse.md`)을 따른다.

| 컬럼 | 타입 | 규칙 |
| --- | --- | --- |
| `dataset_id` | STRING | SQL 리터럴 `'ctr_train_v1'` (실험/데이터셋 버전 식별) |
| `user_id` | STRING | impression에서 추출 |
| `video_id` | STRING | impression에서 추출 |
| `event_timestamp` | TIMESTAMP | impression 이벤트 시각 (PIT 조회 기준 시점) |
| `clicked` | INT64 | 아래 30분 귀속 규칙으로 산출 |
| `source_event_id` | STRING | impression의 `event_id` (전역 고유, 행의 실질 PK) |

- Feast join key(`entity_keys`) = `(user_id, video_id)`. 검증의 유일성 키는
  `(user_id, video_id, event_timestamp)`이며, `source_event_id`가 행의 실질
  식별자다.
- `label_window_sec`(=1800)은 row 컬럼이 아니라 SQL 상수로 전개한다.

### Clicked 귀속 규칙 (정본과 동일)

- 하나의 click은 **직전 30분(1800s) 이내, 같은 `(user_id, video_id)`의 가장 가까운
  (가장 최근) impression 1건**에만 positive를 부여한다(`ROW_NUMBER() ORDER BY
  impression 시각 DESC`, `rn = 1`).
- 그 impression은 `clicked = 1`, 나머지는 `clicked = 0`.
- action log에 impression↔click을 직접 잇는 key(session/request id)가 없어 시각
  기반 근사이며, 이는 `build_training_dataset.derive_wide_events()`의 click 귀속과
  같은 규칙이다.

## 파티션 계약 — 이 테이블만의 3-way 비대칭 ⚠️

`user_dynamic_feature`/`video_feature`는 "대상 날짜 스냅샷"이라 **지우는 범위 = 읽는
범위**가 단일 날짜로 일치했다. `training_entity`는 impression(기준 행)과
click(label 원천)이 **KST 자정을 걸쳐 서로 다른 dt 파티션에 실릴 수 있어**, 세 범위가
분리되는 첫 사례다. 이 비대칭을 혼동하면 label이 조용히 틀어지므로 명시한다.

대상 날짜를 `D`라 할 때:

1. **출력 행(=삭제·검증 범위)** — impression이 **KST 날짜 `D`에 발생한 것만**.
   - `partition_predicate` = `DATE(event_timestamp, 'Asia/Seoul') = DATE 'D'`
   - DELETE와 적재 후 검증이 모두 이 조건을 써서 "이 파티션이 소유하는 행"을
     정확히 짚는다. impression은 정의상 하나의 KST 날짜에만 속하므로 파티션 간
     중복이 없다.

2. **click 스캔 범위** — `D`의 impression에 귀속될 수 있는 click은 impression
   30분 뒤까지이므로, 23:30~23:59 impression의 click이 다음 날 00:00~00:29(`D+1`)에
   실릴 수 있다.
   - clicks CTE: `dt BETWEEN D AND D+1`, `event_timestamp`는
     `[D 00:00 KST, D+1 00:00 KST + 1800s)` 범위.

3. **귀속 후보 impression 범위** — 여기가 #246에 없던 정확성 포인트다.
   click의 "가장 가까운 impression"을 **`D`의 impression만 후보로 두면 오탐이
   난다**. 예: 같은 (user, video)가 `D` 23:55와 `D+1` 00:05에 각각 노출되고 click이
   `D+1` 00:10에 오면, 진짜 귀속 대상은 더 최근인 `D+1` 00:05 impression이다.
   그런데 `D`만 후보로 두면 click이 `D` 23:55 impression에 잘못 붙어 **`D`의 행이
   거짓 positive**가 된다(그리고 `D+1` 빌드가 같은 click을 `D+1` impression에
   정상 귀속시켜 **이중 positive**까지 발생).
   - 따라서 귀속 계산의 **후보 impression은 `dt BETWEEN D AND D+1`**로 넓히고,
     `rn = 1`(전역 최근)을 고른 뒤 **출력은 (1)의 `D` 행으로만 제한**한다. `D+1`
     impression이 승자가 되면 그 positive는 `D+1` 빌드가 소유하므로 이 파티션
     출력에는 나타나지 않는다(무해).
   - 하한(`D-1` impression)은 후보에 넣지 않아도 된다: `D` 00:00 이후의 click에
     대해 `D`의 impression이 존재하면 그것이 어떤 `D-1` impression보다 항상 더
     최근(=진짜 최근)이고, `D`의 impression이 없으면 그 click의 귀속은 `D-1`
     impression → `D-1` 파티션 빌드(clicks `D-1..D` 스캔)가 소유한다. 어느
     경우든 `D` 출력 행에 오탐/누락이 생기지 않는다.

요약: **출력 = dt D / click 스캔 = dt D∪D+1 / 귀속 후보 impression = dt D∪D+1**,
삭제·검증 = impression on day D.

## 빌드 타이밍

(2)·(3)이 `D+1` 파티션을 읽으므로, cross-midnight click을 온전히 잡으려면
`training_entity`의 `D` 빌드는 **`D+2` 이후**에 도는 것이 안전하다. 이는 #295의
소비 계약(`dt ≤ P-1`, 어제까지만 소비)과 정합한다. 자정 근처 소수 지연을 감수하고
`D+1`에 돌리면 그 impression들의 label이 다음 실행 전까지 일시적으로 낮게 잡힐 수
있으나, 같은 날짜 재실행이 멱등(DELETE+INSERT)하므로 `D+2` 재실행으로 교정된다.

## 비범위

- FeatureService 정의·`get_historical_features()` 조회 전환은 #299 Phase 2 범위.
- view/like 체이닝, watch_time_sec 파생은 `training_entity`의 일부가 아니다(정본의
  "16컬럼 파이프라인 전용 임시 어댑터" 노트 참조) — spine은 `clicked` label까지만
  책임진다.
- DAG·스케줄 배선은 `Autoresearch-airflow`, 테이블 스키마는 `Autoresearch-infra`.

## 검증 계획

- `test_feature_store_build.py`에 `training_entity` 커버리지 추가:
  `FEATURE_TABLES` 목록·컬럼·`partition_predicate` assertion, cross-midnight click
  귀속(정상 1건 / 자정 넘긴 1건 / D+1 더 최근 impression에 의한 오탐 방지)을
  SQL 생성 단위로 검증.
- `--dry-run`으로 BigQuery 문법 검증.
- 기존 `build_feature_tables.py` 잔재(테스트 포함) 삭제 확인.
