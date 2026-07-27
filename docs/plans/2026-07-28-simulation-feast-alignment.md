# #359 시뮬레이션 피처 소스 정합 + DuckDB 재계산 제거 (계획)

> [EPIC] #299 Phase 3 · 선행 #356/#357/#358 완료 · 2026-07-28

## 목표

학습·시뮬레이션·일일추천이 **동일한 피처 출처(offline feature store, PIT)**를
쓰게 정렬하고, DuckDB 자체 재계산 경로를 제거한다. train-serve skew의 마지막
원인(제3의 raw 재계산 경로)을 없애고, MLflow run에 데이터셋 lineage를 남긴다.

## 현황 — 피처 경로 5갈래 (조사 대조 결과)

| 경로 | 소비자 | 조립 방식 | 출처 | #359 |
| --- | --- | --- | --- | --- |
| 학습-구 | `build_training_dataset --assembly-source duckdb` | `assembly.py` DuckDB 재계산 | raw BQ | **삭제** |
| 학습-신 | `build_training_dataset --assembly-source feast` (#358) | `retrieve_training_features`(get_historical_features PIT) | offline BQ(정본 #357) | **기본으로** |
| 실시간 서빙 API | `serving/app.py` | `feast_reader.get_online_features` | online Redis | 유지 |
| 일일추천 배치 | `daily_recommendations.run_batch` | `build_pool_feature_frame` → `assembly.py` DuckDB | raw BQ | **offline PIT로 이전** |
| 시뮬레이션 | `simulate_policy_round.main` | `build_pool_feature_frame` → `assembly.py` DuckDB | raw CSV/BQ | **offline PIT로 이전** |

- `build_pool_feature_frame`은 `simulate_policy_round.py`에 정의되고
  `daily_recommendations.py`가 그대로 import한다 — 둘은 물리적으로 같은 코드다.
- `assembly.py`의 `compute_video_features / compute_user_offline_features /
  compute_point_in_time_user_features / compute_interaction_columns`는
  학습-구 DuckDB 경로와 위 공유 경로가 함께 쓴다. 이 둘의 소비자가 모두
  사라져야 `assembly.py` 재계산 함수를 지울 수 있다.

## 설계 결정 (확정)

1. **시뮬·일일추천 피처 출처 = offline PIT.** `feast_retrieval.retrieve_training_features`
   (get_historical_features)를 재사용한다. 학습과 완전히 같은 코드·같은 값이라
   학습↔시뮬 skew를 직접 제거하고, 새 추상화를 만들지 않는다(#358 코드 재사용).
   online(get_online_features)이 아닌 offline을 택한 이유: 학습이 offline PIT를
   쓰므로 정렬 대상이 offline이어야 skew가 사라지고, PIT라 임의 as_of에 정확하며,
   online은 최신 materialized 값만 있어 과거 as_of 리플레이 불가 + Redis
   재-materialize 의존(현재 인프라 OOM으로 막힘)이 생긴다.

2. **daily_recommendations도 함께 이전.** 시뮬만 옮기면 지금 같은 코드였던
   시뮬↔일일추천 사이에 **새 skew**가 생긴다. 둘을 함께 옮겨야 `assembly.py`
   재계산 함수를 온전히 제거할 수 있다("DuckDB 재계산 제거" 완주 조건).

3. **서빙(시뮬/일일추천) 결손 후처리 ≠ 학습.** 조회 함수는 공유하되 후처리는
   갈린다:
   - 학습: `drop_user_dynamic_gap_rows`(활동 유저를 신규 유저로 위장 방지, #357
     (C) 결손 가시화) **후** `apply_cold_start_defaults`.
   - 서빙: `apply_cold_start_defaults`**만**. 콜드 유저의 후보를 드롭하면 그
     영상이 순위에서 조용히 빠진다 — 서빙은 모든 후보를 채점해야 하고, online
     서빙이 콜드 유저에 하는 것과 같은 규칙(0/unknown 채움)을 그대로 따른다.

4. **#224 자연 해소.** offline `days_since_upload`는 `collected_at` 기준
   (`feature_store_build.py:257`, 스냅샷별 저장 → event-time 정합), DuckDB는
   `datetime.now()`(run-date) 기준(`assembly.py:172` + `build_training_dataset.py:589`).
   DuckDB 경로 제거 후 학습이 offline 값을 PIT(ASOF trending date)로 읽으면
   버그가 사라진다. 차이는 `diff_feature_contract.py:291`에 이미 특성화돼 있다.

5. **A1 함수 배치 = 나란히 신규 함수** (기존 시그니처 대체 아님, 확정 2026-07-28).
   `build_pool_feature_frame`(raw 3종 입력)을 지우지 않고 offline PIT용 신규 함수를
   나란히 둔다. 롤백 안전성(구 경로가 A 단계 동안 살아 있음)이 크고, 대체 방식의
   시그니처 충돌을 피한다. 대가로 C2에서 구 함수 제거 시 **두 소비자(시뮬·일일추천)가
   모두 신규 경로로 옮겨졌는지 grep 검증**이 한 번 더 필요하다 — C2 체크리스트에 명시.

**A2 blast radius (조사 확정 2026-07-28):** `simulate_policy_round.main`의 저장소 내
유일한 호출자는 자기 `_cli()` + 테스트다. `daily_recommendations`는 헬퍼 2개
(`_to_candidate_videos`, `build_pool_feature_frame`)만 import하고 `main`은 안 부른다.
`click_threshold_calibrate`(공개 job)는 draft parquet **산출물**만 소비한다.
`autoresearch/jobs/`에 policy-round 래퍼는 없다 → CLI 계약의 외부 노출점은 **인접
저장소 Airflow DAG**뿐(여기서 grep 불가). 따라서 CLI 인자 변경은 저장소 내부는
안전하고, Airflow DAG 태스크 인자만 경계 조율 대상이다.

## 순서 (실측 게이트 — 반드시 지킬 것)

"DuckDB 제거"는 학습 1.77M 전량 조립의 폴백을 없애는 것이라, **feast 경로가
1.77M에서 메모리·정확성을 버티는지 실측한 뒤에만** 지운다(#376 리뷰 지적).

- **Phase A (blocked 아님)** — 시뮬·일일추천 offline PIT 정렬 + MLflow lineage.
  시뮬/일일추천 entity_df는 (유저 × 일일 pool)로 1.77M보다 훨씬 작아 메모리
  게이트와 무관. offline BQ만 읽고 Redis에 의존하지 않으므로 #358 배포 미완
  (Redis 재-materialize)에도 막히지 않는다.
- **Phase B (대장님 BQ 환경 필요)** — 1.77M 전량 feast 경로 메모리·정확성 실측.
  `experiments/2026-07-28_feast-1p77m-memory/`에 baseline 기록.
- **Phase C (B 통과 후에만)** — `build_training_dataset` DuckDB 경로 삭제 +
  기본 feast, `assembly.py` 재계산 함수 제거(Phase A로 공유 경로가 이미
  이전됨), #224 자연 해소 확인.

## 작업 분해

### Phase A — 시뮬·일일추천 정렬 + lineage (선행)

- [x] A1. 서빙용 offline PIT pool 조립 **신규 함수**(§설계결정 5, 기존 함수 대체 아님).
  - `feast_retrieval.build_pool_feature_frame_feast(store, user_id, candidate_video_ids, as_of, *, service)`
    신설. spine = `(user_id 상수, video_id=각 후보, event_timestamp=as_of를 tz-aware
    UTC로 정규화 — 학습 spine과 정렬)`.
  - `retrieve_training_features(store, spine)` 호출 → 누락 피처 가드 →
    `apply_cold_start_defaults`**만** 적용(§설계결정 3, `drop_user_dynamic_gap_rows`
    금지). `video_id` + 21피처 반환(`_to_candidate_videos` 입력 형태).
  - 배치 위치: `feast_retrieval.py`(모듈 docstring을 학습·서빙 공용으로 갱신).
  - 테스트: `tests/test_build_pool_feature_frame_feast.py`(dev-runnable, fake store
    monkeypatch) — spine 구성·tz·서빙 후처리(cold-start만, gap 드롭 금지) 회귀
    가드·누락 가드. 3 passed, ruff 통과.
- [x] A2. `simulate_policy_round` — reranker 입력 조립을 A1 신규 함수로 전환(병존).
  - `main(..., assembly_source="duckdb", feature_store=None)` 추가. feast면 모델
    reranker의 21피처만 `build_pool_feature_frame_feast`로 만들고, baseline
    휴리스틱·LLM 후보 provider·pool 정체(`video_by_id`)는 두 경로 공통. 기본
    duckdb라 기존 동작·테스트 불변.
  - `_cli()`: `--assembly-source duckdb|feast`(기본 duckdb). feast면 `_assemble_via_feast`와
    같은 offline 전용 store를 env(GCS_REGISTRY_PATH/GCS_STAGING_LOCATION +
    BIGQUERY_PROJECT/DATASET)로 만들어 주입.
  - **backward-compat**: `--personas/--events`는 feast에서 모델 피처에 안 쓰이지만
    구 인자 파싱을 남겨 인접 Airflow DAG를 즉시 깨지 않는다. 스위치 유지 vs 전면
    전환(구 인자 제거)은 DAG 호출 인자 확인 후 별도 확정.
  - 테스트: feast 모드가 offline PIT로만 라우팅(duckdb 경로 미호출 assert)·store
    주입 전달·pool 전량 전달·feature_store 누락 가드. 35 passed.
- [ ] A3. `daily_recommendations.run_batch` — `build_pool_feature_frame` 호출을
  A1 신규 함수로 전환.
  - [ ] A3-1. **snapshot_date → as_of 매핑 1건 실측 대조**(체크박스). 현재
    일일추천은 `as_of = events_dt+1`, `snapshot_date = candidate_dt`로 **분리**해
    쓴다(영상 나이 기준일 ≠ 유저 이력 기준일). offline video PIT는 event_timestamp
    하나로 ASOF하므로, 이 분리가 흡수되는지(추천 대상일 영상 스냅샷을 고르는지)를
    실 데이터 1건으로 대조해 회귀가 없음을 확인한다.
- [ ] A4. MLflow dataset lineage. 현재 `train.py`는 `with mlflow.start_run()` 안에서
  `log_parameters(extra_params)`로 문자열 계보만 남긴다(`events_source`/기간).
  `logger.py`는 module-level mlflow 호출의 얇은 래퍼(run 컨텍스트는 호출부가 연다)이므로,
  같은 패턴으로 `log_dataset(df, *, source, name, context="training")` 래퍼를 추가
  (`mlflow.log_input(mlflow.data.from_pandas(df, source=source, name=name), context=context)`).
  기록 내용: FeatureService=`ctr_training_v1`, registry_path, spine 기간, 행 수.
- [ ] A5. 테스트: 시뮬/일일추천이 fake store로 offline PIT 경로를 타는지,
  서빙 후처리가 **drop 없이 cold-start만** 적용하는지(회귀 가드). feast 계열은
  격리 그룹(`uv run --only-group feast`), 나머지는 fake store 주입으로 dev에서 통과.

### Phase B — 1.77M feast 메모리·정확성 실측 (대장님 BQ)

- [ ] B1. `scripts/bench/`에 재현 스크립트 정비(또는 `validate_feast_assembly.py`
  전량 + 계측 래퍼). 측정: 피크 RSS, 조회 시간, spine 대비 손실/NULL율,
  21피처 non-null 비율.
- [ ] B2. 학습-구(duckdb) vs 학습-신(feast) 데이터셋 diff + ROC-AUC 영향
  (n=11일, "참고용" caveat). `diff_feature_contract.py` 활용.
- [ ] B3. `experiments/2026-07-28_feast-1p77m-memory/notes.md`에 Before/After
  기록. **통과 기준 미충족 시 Phase C 착수 금지.**

### Phase C — DuckDB 제거 (B 통과 후)

- [ ] C1. `build_training_dataset` — DuckDB 경로(Step 1~3, `derive_wide_events`
  제외 여부 검토: 일일추천이 여전히 wide 변환을 쓰는지 확인) 삭제, `main`의
  `assembly_source` 스위치 제거, 기본을 feast로. `--assembly-source` 인자·검증
  분기 정리.
- [ ] C2. `assembly.py` 재계산 함수(`compute_*`) 제거.
  - [ ] C2-1. **소비자 소멸 grep 검증**(§설계결정 5의 대가): Phase A로 시뮬·일일추천이,
    C1으로 학습 경로가 `compute_video_features`/`compute_user_offline_features`/
    `compute_point_in_time_user_features`/`compute_interaction_columns`/
    `compute_user_topic_features`를 더는 안 부름을 grep으로 확인 후 삭제.
  - [ ] C2-2. **dead test 정리**: `tests/test_features_assembly.py`가 `compute_*`를
    직접 테스트함 → 제거/이관. `scripts/diff_feature_contract.py`도 `compute_*`를
    쓰므로(DuckDB↔offline diff 하네스) 제거 후 용도 소멸 — Phase B 종료 후 은퇴 여부 결정.
  - [ ] C2-3. 남는 순수 헬퍼(`derive_preferred_category`/`parse_primary_categories`/
    `extract_keywords_safe` 등)가 다른 곳에서 쓰이면 이동, 아니면 함께 제거.
- [ ] C3. #224 자연 해소 확인 — 학습 데이터의 `days_since_upload`가 이벤트
  시점(trending snapshot) 기준으로 나오는지 실측 1건 대조. #224 코멘트/close.
- [ ] C4. 모듈 docstring 갱신(제거된 경로·책임 반영), 문서 정합
  (`build_training_dataset` docstring의 `--assembly-source` 서술 등).

## 검증

- dev: `uv run python -m pytest -v`, `uv run --no-sync ruff check autoresearch tests tools`
- feast 격리: `uv sync --only-group feast` + CI `pytest (feast group)` 목록
- 대장님 BQ: Phase B 실측 스크립트, Phase A/C 엔드투엔드 1회

## 비범위 / 후속

- 실시간 서빙 API(`serving/app.py`, online) 변경 없음 — offline SSOT에서
  materialized되므로 이미 정렬.
- #358 배포 미완(Redis 재-materialize + 서빙 재배포, category_key 인코딩) —
  인프라 도메인, 별건. Phase A는 offline만 읽어 이에 의존하지 않음.
- numpy base 의존성 직접 선언 — 별도 후속.

## 관련 정본

- spec: `docs/specs/2026-07-27-feature-contract-alignment.md`
- 선행 plan(#358): `docs/plans/2026-07-27-featureservice-retrieval.md`
- EPIC: #299
