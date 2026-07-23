# action log 파티션 시맨틱 통일 — 당일 슬라이스(A안) (#295)

> 작성: 2026-07-24 | 상태: 설계(리뷰 대기) | 관련: #283(트레일링 필터 도입),
> #286(학습 경로 재범위), #287(슬라이스 0-스냅샷 실측), #278(히스토리 조립 예고)

## 결정

`data_lake_action_log`의 파티션 시맨틱을 **당일 슬라이스로 통일**한다
(2026-07-24 확정, 이슈 #295의 A안).

- **저장 계약**: 파티션 `dt=D`는 **KST D일 하루치 이벤트만** 담는다
  (`event_timestamp` ∈ [D-1 15:00Z, D 14:59:59Z]). 파티션 간 이벤트는
  서로소다. `run_daily_action_log`의 "이벤트=파티션 당일" 검증이 이 계약을
  이미 강제하고 있으며 유지한다.
- **소비 계약**: 30일 히스토리가 필요한 소비자는 `dt BETWEEN P-30 AND P-1`
  (파티션 프루닝) + 기존 `event_timestamp` 윈도우로 **여러 슬라이스를 모아
  읽는다**. 슬라이스는 서로소이므로 이 합산은 중복이 아니다.
- **전제**: `event_id` 전역 고유화(아래). 파티션 간 조립이 기본이 되므로
  event_id 충돌은 attribution 오염(1기 실측: CTR 2%→17%)으로 직결된다.

### 기각한 대안 (B안 — 전부 트레일링)

파티션마다 직전 30일 히스토리를 동반 조립해 넣는 방식. 소비는 단순하지만
① 저장 ×30 중복, ② 일일 러너의 당일 검증과 충돌, ③ 매 산출물에 조립
파이프라인 신설 필요, ④ 인접 파티션 혼입 시 중복 집계 지뢰(2026-07-24
실측: pre-#283 빌드가 트레일링 파티션 시기에 스냅샷 클릭합 16,562 vs 기대 6,
~2,760배 부풀림)로 기각한다.

## 실측 근거 (이슈 #295 본문·코멘트에 상세)

| 실측 | 내용 |
| --- | --- |
| 슬라이스 × `dt=P` 필터 | 스냅샷 전량 0 → materialize 시 online을 0으로 덮음 (2026-07-23, KST 07-17~20 4개 복원) |
| 트레일링 × 윈도우 스캔 | 클릭합 16,562 vs 기대 6 (2026-07-24, snap 07-23 15:00Z 정정) |
| event_id 충돌 | **176,646개** event_id가 복수 파티션에 존재, `evt_00000001`은 14개 파티션에 등장 (2026-07-24 BQ 전수) |

## 변경 범위

### 1. `feature_store_build` SQL 계약 (`docs/guides/data-warehouse.md`가 단일 출처)

`user_dynamic_feature`의 `action_log` CTE에서 dt 술어만 교체한다.
`event_timestamp` 윈도우(30일)는 이미 정확하므로 유지한다.

```sql
-- 현행 (#283, 트레일링 가정)
AND dt = DATE '{partition_date}'
-- 변경 (A안, 슬라이스 30일 프루닝)
AND dt BETWEEN DATE_SUB(DATE '{partition_date}', INTERVAL 30 DAY)
           AND DATE_SUB(DATE '{partition_date}', INTERVAL 1 DAY)
```

- KST 정합: 슬라이스 dt=D = KST D일이므로, 윈도우 [P-30 KST 자정, P KST 자정)은
  정확히 dt ∈ [P-30, P-1]에 대응한다.
- 회귀 테스트: #283이 넣은 "단일 dt 술어 존재" 단언을 "BETWEEN 프루닝 술어
  존재" 단언으로 교체하고, 슬라이스 파티션 재현 픽스처로 0-스냅샷이
  발생하지 않음을 검증한다.
- CLI 인터페이스(`--partition-date`)는 불변 — **Airflow DAG 저장소는 무변경**.

### 2. `event_id` 전역 고유화 (`autoresearch/action_logs/`)

- 생성 규칙을 `evt_{seq:08d}`(배치마다 0부터 재시작)에서 **파티션 네임스페이스
  포함 형식** `evt_{YYYYMMDD}_{seq:08d}`(YYYYMMDD = 이벤트 KST 날짜)로 바꾼다.
  결정적(deterministic)이라 재실행 멱등이 유지된다.
- `derive_wide_events` 등 event_id로 impression↔click을 잇는 소비자는 형식에
  무관(불투명 문자열)함을 테스트로 고정한다.
- 스키마 계약(`EVENT_LOG_PARQUET_SCHEMA`)의 문자열 타입은 불변이므로 parquet
  스키마 변경은 없다.

### 3. 학습 경로 (#286 재범위 확정)

`build_training_dataset.py`의 `dt BETWEEN start_date AND end_date`는 A안에서
**정합한 API가 된다** — #286의 위험은 BETWEEN 자체가 아니라 ① event_id 충돌,
② 트레일링 파티션 혼입이었다. 본 spec의 전제(②는 마이그레이션으로 근절,
①은 전역화)가 충족되면 #286은 다음으로 재범위한다:

- `events_start/end_date`의 dt 필터·timestamp 윈도우 **이중 역할 해소**
  (2026-07-23 실측: 트레일링 파티션에서 0행 유발 — A안 후엔 슬라이스 기준으로
  자연 해소되나, 인자 의미를 "이벤트 발생 KST 날짜 범위" 하나로 문서화).
- 소급 event_id 재작성 전의 과거 파티션을 학습에 쓰지 않도록 가드(또는
  마이그레이션 완료를 전제로 명시).

### 4. 폐루프 라운드 산출물 규약

- 라운드 이벤트는 **실제 발생 당일 timestamp 그대로**, `dt=이벤트 당일`로
  업로드한다. 기존 "D일 이벤트 → dt=D+1 트레일링" 관행과 30일 확장
  (`expand_action_log_drafts`의 합성 히스토리 전개) 업로드를 **폐지**한다.
  히스토리는 이전 슬라이스들이 담당한다.
- 업로드 전 대상 dt가 비어 있는지 `gcloud storage ls`로 확인한다(적재는
  WRITE_TRUNCATE 전체 재적재).
- runbook(`docs/guides/`)의 업로드 절차를 이 규약으로 갱신한다.

### 5. 데이터 마이그레이션 (GCS → BQ 재적재 1회)

BQ는 GCS 전체 재적재 산출물이므로 **GCS만 바로잡으면 된다.** 대상은
트레일링 파티션 4개뿐이며, 슬라이스 11개(dt=07-07, 07-12~21)는 event_id
재작성 외 불변이다.

| 파티션 | 실체 (실측) | 처분 |
| --- | --- | --- |
| dt=07-23 (round_a) | 8유저 스모크의 30일 합성 전개 (174행, 06-22~07-22 스팬) | **아카이브** — 재슬라이스 불가(합성 전개), 원본은 `data/generated/round_a/` 보존 |
| dt=07-24 (라운드 N) | **실질 슬라이스** — 256행 전부 KST 07-23 하루 (07-22 15:03Z~07-23 13:50Z) | **dt=2026-07-23으로 재라벨 업로드** (event_id 재작성 포함, 실서버 라운드 실데이터라 보존 가치 있음) |
| dt=07-25 (R0) | 100유저 라운드의 30일 합성 전개 (2,510행, 06-24~07-24 스팬) | **아카이브** — R1 전개와 이벤트 기간이 겹쳐 재슬라이스 시 밀도 ×2 오염, 원본은 `data/generated/exp_r0/` 보존 |
| dt=07-26 (R1) | 92유저 라운드의 30일 합성 전개 (2,313행, 06-25~07-25 스팬) | **아카이브** — 상동, 원본은 `data/generated/exp_r1/` 보존 |

- 아카이브 위치: `gs://{bucket}/archive/action_log_trailing/dt=*` (data_lake
  경로 밖 — 재적재 와일드카드에 잡히지 않음) + 로컬 백업.
- 순서: GCS 정리 → `load_raw_to_bigquery --tables action_log` 재적재 → dt별
  행수·클릭수 검산(2026-07-24 검산 쿼리 재사용).
- v5 모델 학습 재현성: R0 학습은 로컬 CSV 경로(`events_source=csv`)로 수행돼
  레이크와 무관하므로 영향 없다.

### 6. 파생 스토어 정합화

- **offline (`feast_offline_store.user_dynamic_feature`)**: 트레일링 기준으로
  생성된 스냅샷 4개(event_timestamp 07-22Z~07-25Z)를 마이그레이션 후 새 계약으로
  **재빌드**한다(멱등 DELETE+INSERT). 값이 달라지는 것이 정상이다(합성
  슬라이스 + 07-23 라운드 슬라이스 기준으로 재계산).
- **online (Redis)**: 재빌드 후 최신 partition-date를 materialize한다.
  Feast는 과거 timestamp를 무시하므로 **재빌드 전 materialize 금지**
  (0 또는 구값으로 덮으면 복원 불가).

### 7. 문서 정정

- `docs/guides/data-warehouse.md`: SQL 본문(위 §1)과 "각 dt는 독립적인 30일
  히스토리이므로 대상 파티션 하나만 읽는다" 주석·서술 전부 교체.
- #283 spec(`2026-07-22-feature-store-build-batch.md` 갱신분)과
  `feature_store_build.py` 모듈 docstring의 파티션 계약 서술 정정.
- CLAUDE.md·메모리성 문서의 "UNION 금지" 문구는 "슬라이스 계약 + event_id
  전역 고유 전제 하에 BETWEEN 조립이 표준"으로 정정.

## 비목표

- Airflow DAG·schedule 변경 (인접 저장소 무변경이 본 설계의 제약 조건)
- champion 서빙(#271) 관련 작업
- 슬라이스 합성 파티션 12개의 내용 재생성 (event_id 재작성만 수행)
- 실시간(스트리밍) 피처 경로 — 일 단위 snapshot 계약(MVP)은 불변

## 완료 조건 (이슈 #295 체크리스트에 대응)

- [ ] `feature_store_build` BETWEEN 전환 + 슬라이스/경계 회귀 테스트
- [ ] event_id 전역 고유화(생성 규칙) + 소급 재작성 스크립트 + 충돌 0 검증
- [ ] 트레일링 파티션 4개 처분(§5 표) + BQ 재적재 + 검산
- [ ] offline 스냅샷 4개 재빌드 + online 재 materialize + 일치 검증
- [ ] 문서·runbook 정정 (§7)
- [ ] #286 재범위 반영(이슈 코멘트) 및 후속 착수

## 구현 순서 메모

코드 변경(§1·§2)과 데이터 마이그레이션(§5·§6)은 **코드 먼저** 순서로
진행한다 — BETWEEN 빌드가 머지되기 전에 트레일링 파티션을 지우면 그 사이
빌드가 전부 0-스냅샷이 된다. 상세 작업 분해는 별도 plan
(`docs/plans/2026-07-24-action-log-slice-semantics.md`)에서 다룬다.
