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
  - `retrieve_training_features(store, spine)` 호출 → 누락 피처 가드 → **후보 순서로
    reindex**(PR #377 리뷰 1) → `apply_cold_start_defaults`**만** 적용(§설계결정 3,
    `drop_user_dynamic_gap_rows` 금지). `video_id` + 21피처 반환.
  - reindex가 서빙 계약(no-drop + 결정론적 순서)을 **store 구현과 무관하게** 관철:
    `get_historical_features`는 ORDER BY가 없어 조회 순서가 흔들리면 exploration·tie-break·
    replay 정합이 깨지고(리뷰 1), File store가 드롭한 행/미발견 영상은 NaN으로 복원돼
    cold-start된다 → §설계결정 3이 BQ뿐 아니라 함수 레벨에서 성립.
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
- [x] A3. `daily_recommendations.run_batch` — feast 경로 추가(`--assembly-source duckdb|feast`,
  기본 duckdb 병존). feast면 `build_pool_feature_frame_feast`로 **단일 as_of=candidate_dt+1**,
  events 로드 생략, store는 주입 또는 env로 구성. 공개 batch 계약(v1 선택 인자 추가=비-breaking),
  계약 spec의 daily 섹션에 `--assembly-source`+feast env·이미지 문서화. 모듈 docstring 갱신.
  테스트: feast 라우팅(duckdb 미호출 assert)·as_of=candidate_dt+1·env 가드. 23 passed, CLI
  `--help/--version/invalid` 계약 확인.
  - [x] A3-1. **as_of 실측 대조 완료** (`scripts/bench/daily_as_of_probe.py`). 결론:
    **단일 as_of=candidate_dt+1로 충분** — 영상 PIT가 candidate_dt 스냅샷을, 유저 PIT가 그 이하
    최신 UserDynamic(=events_dt, 60h ttl 안)을 골라 duckdb의 2기준 분리를 흡수. days_since_upload는
    스냅샷 저장값(event-time)이라 #224 자연 해소 근거도 확인. edge(ttl>60h 지연)는 cold-start+관측 후속.
- [x] A4. MLflow dataset lineage. `logger.py`에 `log_dataset(df, *, name, source,
  context, targets, tags)` 래퍼 추가(`mlflow.data.from_pandas` + `mlflow.log_input`,
  기존 얇은 래퍼 패턴). `train.py`가 dataset 로드 직후 이를 호출해 run의 Datasets
  섹션에 학습 데이터셋을 input으로 남긴다 — provenance는 input **태그**로:
  행 수 + extra_params(피처 소스·기간·assembly_source) + (feast면) FeatureService·
  registry_path. `cli.py run_pipeline`이 feast일 때 `data_source_params`에
  `feature_service=ctr_training_v1`·`feast_registry_path`(env)를 추가. extra_params가
  없는 standalone train은 행 수만 남긴다. params 기록(기존)은 유지 — dataset lineage와
  병존. 테스트: run inputs에 `training_dataset` + context/provenance/행수 태그 확인.
- [x] A5. 테스트.
  - dev(fake store 주입): `build_pool_feature_frame_feast` 로직(A1)·시뮬 feast 라우팅(A2)·
    서빙 후처리 **drop 없이 cold-start만** 회귀 가드. `uv run` dev에서 통과.
  - feast 격리 그룹(로컬 File store): `test_feast_retrieval_integration_feast.py`에
    `build_pool_feature_frame_feast` end-to-end 추가 — 실물 Feast API로 staged 조회가
    물리적으로 맞물려 도는지(fake-store 간극) + 반환 계약. CI `pytest (feast group)`가
    파일 전체를 돌려 자동 포함.
  - **실측 발견**: File store는 미발견 엔티티가 섞인 다중 뷰 PIT 조회에서 행을 드롭한다
    (spine 2행 중 1행 손실 실측). → **A1 reindex(PR #377 리뷰 1)로 해소**: 후보 순서로
    reindex하면 드롭 행도 NaN으로 복원돼 cold-start되므로 §설계결정 3이 File store에서도
    성립. 통합 테스트를 present-only에서 **present+미발견 혼합 no-drop 검증**으로 강화.
    B2-1은 "정합성 필수 검증"에서 "성능/의미 확인"으로 격하.

### Phase B — 1.77M feast 메모리·정확성 실측 (대장님 BQ)

- [ ] B1. `scripts/bench/`에 재현 스크립트 정비(또는 `validate_feast_assembly.py`
  전량 + 계측 래퍼). 측정: 피크 RSS, 조회 시간, spine 대비 손실/NULL율,
  21피처 non-null 비율.
- [ ] B2. 학습-구(duckdb) vs 학습-신(feast) 데이터셋 diff + ROC-AUC 영향
  (n=11일, "참고용" caveat). `diff_feature_contract.py` 활용.
- [ ] B2-1. **서빙 pool no-drop + cold-start를 BigQuery로 검증**(성능/의미 확인으로 격하).
  A1 reindex로 반환 계약(순서·개수)은 store 무관하게 보장되므로, BQ에서 남는 확인은
  "reindex가 BQ 결과에도 자연스럽게 맞물리는지"와 cold-start 채움 비율의 의미다.
- [ ] B3-daily. **일일추천 feast 조회 비용 측정·배치화**(PR #381 리뷰 4) — **feast 기본 전환(Phase C) 전 필수**.
  daily는 유저마다 `build_pool_feature_frame_feast` 호출 → staged 2회 = vu_1000 기준 하루 ~2N(2000)
  BQ 잡(각 entity_df 업로드). Phase B 실측(217s/4.36GB)은 단일 대형 spine이라 이 경로를 대표 못 함.
  pool이 전 유저 공통이므로 (전 유저 × 전 후보) spine 1회 조회 후 user_id 그룹핑으로 접을 수 있다
  (1000×200=20만 행 1회). 이 경로 지연/슬롯 비용 실측 후 배치화.
- [ ] B3. **시뮬 라운드 feast 조회 비용 측정**(PR #377 리뷰 3). `_model_feature_frame`이
  유저당 호출 → staged 2회 조회(유저 N명 → ~2N BQ 잡), 특히 stage 1(영상 category,
  키=(pool, as_of))은 전 유저 동일한데 반복. N=1000 기준 잡 수·업로드·소요를 실측하고,
  임계 초과 시 (user×pool) 곱집합 spine 1회 조회로 묶는 배치화를 검토(후속 여부 결정).
- [ ] B3. `experiments/2026-07-28_feast-1p77m-memory/notes.md`에 Before/After
  기록. **통과 기준 미충족 시 Phase C 착수 금지.**

### Phase C — DuckDB 제거 (B 통과 후, **3-PR 분할, 범위 (a)**)

**범위 확정 (2026-07-28):** #359는 **C1+C2까지**로 완결한다 — 학습·시뮬 DuckDB 제거 + 기본 feast.
daily의 feast-only 전환은 공개 batch 계약 breaking(v2 + Autoresearch-airflow 이미지·env·DAG 조율)이라
**#359 밖의 새 이슈(#299 아래)**로 분리한다. 되돌리기 쉬운 C1과 어려운 C2를 물리적으로 다른 PR로 둔다.

**#359 close 시 corrigendum:** EPIC 제목 "DuckDB 재계산 경로 제거"는 학습·시뮬에서 **달성**,
daily는 feast 경로가 이미 정렬돼 있고 default만 미전환(신규 이슈로 이관)임을 close 코멘트에 명시한다
(silent partial-completion 금지).

#### C1 (PR 1) — 전제: 삭제 없음, 되돌리기 쉬움
- [x] C1-1. **daily feast 조회 배치화**(리뷰 4/B3-daily): 유저마다 staged 2회(하루 ~2N BQ 잡) →
  (전 유저 × 전 후보) spine 1회 조회 후 `user_id` 그룹핑. `build_pool_feature_frames_feast` 신설,
  daily가 루프 전 1회 호출. config 오류 fail-fast(리뷰 #341). dev 63 + feast 2 passed. **BQ 검증**:
  daily `--assembly-source feast --dry-run`이 실 BQ(1000×200)에서 1회 조회로 도는지(대장님).
- [x] C1-2. **메모리: 4.36GB 수용, chunking은 future 레버로 문서화**(측정 없는 선제 최적화 지양,
  프로젝트 원칙). 정확 사용률 = 4.36GB(십진) / 5.88Gi(=6.31GB 십진) ≈ **69%**(순진 비교 74%) —
  "26% 여유"가 아니라 **2/3 초과 타이트**. 한도까지 ≈1.45배 데이터(≈16 정상일). **chunking 트리거:
  피크 사용률 85%(≈5.4GB) 또는 spine ~2.2M 행 초과** → `experiments/2026-07-28_feast-1p77m-memory/notes.md`.
  학습 assembly 경로는 C1에서 미변경(C1-1은 daily만) → 4.36GB 그대로 유효.
- [x] C1-3. 삭제 없음 → duckdb 폴백 유지한 채 병존. C2에서 삭제.

#### C2 (PR 2) — 삭제: 되돌리기 어려움, 공개 계약 무관
- [ ] C2-0. **착수 전 blast radius 확인**(A2 패턴): in-repo 호출자는 `src/cli.py`뿐(build-features/
  run-pipeline typer 기본값). **Autoresearch-airflow 학습 DAG가 `--assembly-source`를 명시로 넘기는지
  (기본 전환 무영향) / 기본에 의존하는지(영향)** 확인 후 cli.py 기본을 feast로 전환.
- [ ] C2-1. `build_training_dataset` DuckDB 경로 삭제 + 기본 feast. duckdb 전용 함수
  (`load_videos_from_bigquery`/`load_events_from_bigquery`/`load_user_category_similarity_from_bigquery`/
  `derive_wide_events`/`padded_dt_range`/`events_kst_window`/`validate_events`/Step 1~3, `assembly_source`
  스위치)를 정리. cli.py `--assembly-source` 정리.
- [ ] C2-2. `simulate_policy_round` duckdb 분기 제거(feast-only): `build_pool_feature_frame`(duckdb) +
  `assembly` import 제거, `--assembly-source` 스위치 제거.
- [ ] C2-3. **`assembly.compute_*`는 제거하지 않는다(확정)** — daily_recommendations가 여전히
  `build_pool_feature_frame`로 소비. **최종 제거는 C3(별도 이슈) 완료 후.** daily만 쓰게 된
  `build_pool_feature_frame`은 `simulate_policy_round`에서 daily가 접근 가능한 위치로 남기거나 이동만.
- [ ] C2-4. 도구 은퇴: `scripts/diff_feature_contract.py`(#357 diff 하네스, 목적 완료)·
  `scripts/bench/bench_feature_assembly.py`(duckdb 벤치, 구식) 제거. dead 테스트 정리
  (`tests/test_features_assembly.py`는 compute_*가 C3까지 남으므로 **유지**; duckdb 경로 전용 테스트만 정리).
- [ ] C2-5. **#224 close** — 학습 `days_since_upload`가 이벤트 시점(offline snapshot) 기준임을 실측 1건
  대조 후 #224 close. 모듈 docstring·문서(`--assembly-source` 서술) 정합.

#### C3 (별도 새 이슈, #299 아래, cross-repo) — #359 밖
- daily feast-only 전환: 공개 batch 계약 v2 + Autoresearch-airflow(feast 이미지·env·DAG) 조율.
- 이때 **`assembly.compute_*`·`build_pool_feature_frame`·잔여 duckdb·`test_features_assembly.py` 최종 제거**.
- 신규 이슈 발행 시 cross-repo 담당 배정 앵커로 사용.

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
- **cold-start 채움 비율 관측**(PR #377 리뷰 4) — materialize 미완 시 전 유저 UserDynamic
  NULL → 전면 cold-start된 무의미 점수가 event log로 재학습에 되먹임될 수 있음. 서빙이
  "드롭 대신 채움"을 택한 대가로 채운 비율을 리포트 필드로 계측하거나 임계 초과 fail-fast.
  per-user 함수에서 집계를 끌어올려야 해 별도 후속(관측 장치).

## 관련 정본

- spec: `docs/specs/2026-07-27-feature-contract-alignment.md`
- 선행 plan(#358): `docs/plans/2026-07-27-featureservice-retrieval.md`
- EPIC: #299
