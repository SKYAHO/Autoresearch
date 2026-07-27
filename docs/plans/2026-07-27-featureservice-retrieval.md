# 계획: FeatureService 정의 + 학습 조립 get_historical_features 전환 (#358, Phase 2)

- Issue: #358 (부모 [EPIC] #299), 선행 #357(계약 정렬·정본 채택) 완료(PR #369 머지)
- 이 브랜치에서 함께: **#357 (C) ttl=60h 확정** 반영 + 스펙 마감 → #357 close

## 상태 (2026-07-27 마감)

작업 1~5 구현 + 작업 6 **실환경 검증 완료**(`validate_feast_assembly.py`로 실 BigQuery):

- ttl(1)·ODFV(2)·FeatureService(4)·staged 조회(3)·`--assembly-source`(5) 전부 구현·테스트
- 실환경에서 잡아 고침: **category_key 이름충돌**(BigQuery offline ambiguous), **cold-start
  null 처리**(video 미발견 ~3.2%)
- 실측 확인: 21피처 정본 조회 + cold-start 후 non-null 1.0, ttl 드롭 0, ODFV scale 문제없음
  (오버헤드-바운드)
- **Task 5(ROC-AUC)는 후속으로 미룸** — n=11 참고용이라 결론 무관, duckdb 실데이터 셋업
  번거로움. 정확성은 위로 충분히 검증됨.

## 목표

학습 21피처를 **FeatureService**로 정의하고, `build_training_dataset` 조립을
**Feast `get_historical_features`(PIT 조회)** 경유로 교체한다. 구 DuckDB 재계산
경로는 `--assembly-source duckdb|feast`로 **병존**시켜 롤백 여지를 남긴다(#359에서
DuckDB 제거).

이 단계로 "서빙이 읽는 값 = 학습이 배우는 값"을 코드로 성립시킨다 — #357은 두
경로가 다름을 실측했고 offline을 정본으로 정했으며, 여기서 학습을 그 정본으로
옮긴다.

## 결정 반영 (#357 산출)

- **(A) 파생 2종 → ODFV**: `preferred_category_match`, `historical_category_match`
- **(B) cross-entity → staged 조회**: video PIT로 category 확정 → similarity 조인
- **(C) ttl → 60h** (아래 작업 1에서 확정 근거·범위 명시)

## 작업

### 1. FeatureView ttl (결정 C 확정)

- **`UserDynamicView` = `ttl=timedelta(hours=60)`**
  - 당일 임프레션 최대 24h + 1일 결손(≤48h)까지 stale 허용, 2일+ 결손은 null
  - #365 결손이 잦아 1일마다 null 내면 학습이 과도하게 빔 → 60h로 stale 상한을 묶음
- **`UserStaticView`, `UserCategorySimilarityView` = `ttl=None`**
  - 갱신 주기 불규칙(persona/임베딩 변경 시만)이라 배치 주기 기반 ttl 성립 안 함. 60h면 정상인데도 매일 null(가짜 경보)
- **`VideoFeatureView` = `ttl=None`** (모델링 결정)
  - 트렌딩 이탈은 "피처 없음"이 아니라 "인기 식음" 신호일 수 있어 마지막 스냅샷 유지
  - 트레이드오프: view_count 등이 마지막 트렌딩 시점 값으로 고정(days_since_upload도 안 자람) — spec에 근거 명시. 모델링 재검토 여지 열어둠
- `test_offline_retrieval_smoke_feast.py` 갱신:
  - 현재 `test_missing_day_falls_back_to_stale_snapshot_without_ttl`(주석: "#357에서 ttl로
    막을 대상")을 **ttl 도입 후 동작**으로 바꿈 — 60h 안 결손은 stale 서빙, 60h 초과는 null.

### 2. ODFV — 파생 2종 (결정 A)

- `feast.on_demand_feature_view`로 `preferred_category_match` / `historical_category_match` 정의
  - 입력: `preferred_category`(UserStatic), `historical_category_affinity`(UserDynamic),
    `category_id`(Video)
  - 본체는 `src/features/feature_builder.py`의 `compute_preferred_category_match` /
    `compute_historical_category_match` **재사용**(학습·서빙 공통 변환으로 skew 차단)
- 주의: feature_repo가 `src.features`를 import 가능한지 경로/의존 확인(현재 feast 격리 그룹).

### 3. Staged 조회 — topic_similarity (결정 B)

- `topic_similarity`는 (user, **영상 category_id**) 키라 닭-달걀 → 2단계:
  - 1차: `VideoFeatureView`로 video PIT 조회 → 각 (video_id, event_timestamp)의 `category_id` 확정
  - 2차: 확정된 `category_id`를 entity_df에 붙여 `UserCategorySimilarityView` PIT 조회
- `get_historical_features` 단일 호출로 될지 2호출로 나눌지 실측 결정(1차 결과를 entity_df에
  머지 후 2차 호출이 안전한 기본).

### 4. FeatureService 정의

- 21피처 학습셋을 하나의 `FeatureService`로 묶음:
  - UserStatic 3 + UserDynamic 6 + Video 9 + UserCategorySimilarity 1 + ODFV 2 = 21
  - `src/features/model_contract.py`의 `MODEL_FEATURE_COLUMNS`와 이름·개수 1:1 대조(계약 검증).

### 5. build_training_dataset `--assembly-source`

- `duckdb`(기본, 현행) | `feast`(신규) 스위치 추가:
  - feast 경로: spine=`training_entity`를 entity_df로 `get_historical_features` 호출 →
    21피처 PIT 조회 → 기존 CSV 출력 계약 유지
  - duckdb 경로: 현행 그대로(#359까지 병존)
- 경로 분기는 조립부에 국한, 다운스트림(train.py, 출력 스키마)은 불변.

### 6. 검증 — "구경로 일치"의 재해석

- **주의**: #358 이슈 체크박스 "Feast 경로 값이 구경로(DuckDB)와 허용오차 내 일치"는
  **#357이 이미 반증함**(총 불일치 69% 등). feast=offline 정본이라 **duckdb와는 의도적으로 다름**.
  - 따라서 검증 기준을 바꿈:
    - (a) **feast 경로 값 == offline 테이블 값**(정본과 일치, 구조상 성립해야 함)
    - (b) feast 경로로 **학습 1회 완주**(feast 격리 이미지)
    - (c) **Task 5**: feast/duckdb 데이터셋 각각 학습해 Val/Test ROC-AUC 비교(n=11 참고용)
  - duckdb와의 차이는 "버그"가 아니라 #357에 기록된 정본 채택 결과임.

### 7. #357 스펙 마감 + close

- `docs/specs/2026-07-27-feature-contract-alignment.md`의 (C)를 "확정: ttl=60h"로 갱신,
  정적 뷰 예외·video 열린질문 기록.
- #357 close(이 브랜치 PR에서 (C) 확정을 담으므로).

## 검증

- `uv run python -m pytest`(스모크 포함) / `ruff`.
- feast 계열: `uv sync --only-group feast`에서 `pytest (feast group)` 목록 실행.
- feast 격리 이미지 학습 1회(실 GKE/BQ, 대장님 환경) — CI에선 로컬 File store 스모크로 대체.

## 산출물

- `feature_repo/feature_definitions.py`: ttl + ODFV + FeatureService
- `build_training_dataset.py`: `--assembly-source` 분기
- 갱신된 스모크 테스트, #357 스펙 (C) 마감

## 실측 발견

- **(작업 1) beyond-ttl은 행 드롭(0행), NaN 아님**: `get_historical_features`가 ttl 밖
  엔티티를 NaN 행이 아니라 **결과에서 제외**함(feast file store 스모크 실증). → 다중 뷰
  FeatureService 조인에서 한 뷰라도 ttl 밖이면 그 임프레션이 **학습셋에서 통째로 빠질** 수
  있음. 작업 5(assembly)에서 spine(training_entity) 행 수 대비 조회 결과 행 수를 반드시
  검증(60h ttl인 UserDynamic이 2일+ 결손일 때 임프레션 손실 여부).
- **(작업 2/6) ODFV scale — 문제없음(실측 해소)**: "scale 안 됨" 경고는 떴지만 실측은
  **오버헤드-바운드**였다: 2,000행 29.5s / 100,000행 38.2s(50배인데 +9s). 대부분 고정
  오버헤드(entity_df 업로드 + staged 2쿼리 + GCS staging)라 1.77M도 ~분 단위 예상 →
  **ODFV 유지**. (서빙은 어차피 후처리라, ODFV는 offline 전용 편의일 뿐 — 필요하면 후처리
  전환도 skew 손실 없으나 지금은 불필요.)
- **(작업 6) 영상 미발견 null ~3.2% (실환경 실측)**: offline `video_feature` PIT에서 영상
  스냅샷이 없으면(테이블 부재) 그 행의 영상 피처가 null(category_id 0.968, topic_similarity
  0.968). null은 categorical 인코딩을 깨므로 **cold-start 기본값 필수**.
  - 고침: `apply_cold_start_defaults`가 **서빙(online_features)과 같은 규칙**(카테고리
    →'unknown', 수치→0)으로 채움. 카테고리 기본값 상수(`COLD_START_CATEGORICAL_DEFAULT`)를
    서빙에서 단일 소스로 뽑아 학습/서빙 공유(복제 금지 — skew 원천 차단).
  - **데이터 갭 자체는 #358 범위 밖**: feast 3.2% vs DuckDB 0.004%(옛 실측) 차이는 offline
    `video_feature` 테이블이 raw 트렌딩보다 성긴 것(#356 백필 video 18/20일). **알려진
    데이터 갭이고 #356/#365 후속**이지 이 경로의 버그가 아니다. 기록만 하고 넘어간다.
- **(작업 6) `category_id` 이름 충돌 — BigQuery offline 전용 (실환경 검증이 잡음)**:
  `category_id`가 VideoFeatureView **피처**(모델 입력)이면서 category 엔티티 **조인키**를
  겸해, BigQuery offline PIT SQL이 `Column name category_id is ambiguous`로 죽음(Feast
  규칙: 피처명 ≠ 조인키명). **online(Redis key-value)·File 스모크는 SQL이 없어 통과** →
  통합 스모크가 못 잡고 `scripts/validate_feast_assembly.py` 실환경 실행이 잡음.
  - 고침(B안): category 엔티티 조인키를 **`category_key`로 분리**. 물리 BQ 컬럼(category_id)은
    그대로 두고 source `field_mapping={"category_id":"category_key"}`로 매핑. staged
    entity_df·serving 2단계 조회 키도 category_key. **모델 피처 `category_id`(1단계·모델
    계약)는 한 줄도 안 바뀜.** File 스모크에 field_mapping 반영해 재현.
  - **배포 주의**: 엔티티 조인키가 바뀌므로 (1) Feast **재-apply** 필요, (2) Redis online
    키 인코딩이 category_id→category_key로 바뀌어 **재-materialize** 필요(값은 동일).

## 리스크 / 열린 질문

- **정적 뷰 ttl**: user_static / similarity에 60h는 부적합 → None/장기. 확정 필요(작업 1).
- **video ttl 의미**: 트렌딩 이탈 영상이 60h 후 null이 되는 게 맞는지.
- **ODFV에서 src.features import**: feast 격리 그룹의 의존/경로 문제 가능 — 안 되면 변환
  로직을 feature_repo로 옮길지(복제 drift 위험) 결정.
- **staged 조회 성능**: 2호출 + entity_df 머지가 1.77M spine에서 메모리/시간 괜찮은지 실측.
- **표본**: 정상 11일(#365). Task 5 ROC-AUC는 참고용.

## 비범위

- DuckDB 경로 제거 = #359
- online 서빙 null 처리(ttl=60h로 2일+ 결손 시 null) 대응 = 서빙 도메인(효창), 별도 트랙
