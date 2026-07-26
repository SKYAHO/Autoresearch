# 계획: offline store 실적재 검증 + 조회 스모크 (#356)

- Issue: #356 (부모 [EPIC] #299 학습 데이터셋 Feast PIT 조회 전환)
- 선행: spine 복구 #245/#355 완료

## 목표

offline feature store가 실제로 조회 가능한지 검증하고, PIT 전환을 막는 잔재를
정리한다. #299의 나머지 단계(#357 계약 정렬 → #358 조회 전환)가 딛고 설 발판.

## 설계 결정 — 스모크를 어디서 도나 (2층)

실제 `offline_store`는 BigQuery(`feature_store.yaml`)라 CI에서 실 BQ 조회는 불가
(자격증명·네트워크 없음). 그래서 검증을 두 층으로 나눈다:

- **CI 스모크 (로컬 파일 오프라인 스토어)** — tmp 임시 feast repo + `FileSource`
  parquet로 `get_historical_features` PIT 조회를 **실제로 실행**해, 조회 API와
  as-of(PIT) 의미가 우리 FeatureView 스키마에서 동작함을 검증. feast 그룹.
  - **스키마 drift 방지**: 프로덕션 FeatureView는 `BigQuerySource` 하드코딩이라
    그대로 못 쓴다. File 소스 뷰를 별도로 만들되, **컬럼(Field) 목록은
    `feature_repo/feature_definitions.py`의 FeatureView `.schema`에서 import해
    재사용**한다(BQ/File 두 세트가 손으로 어긋나지 않게 단일 소스). 어긋나면
    스모크가 실물과 다른 걸 검증하게 되는 리스크(#357 diff와 같은 클래스).
- **실 BQ 커버리지 측정 (스크립트)** — 실제 BigQuery의 offline 테이블 적재 기간·
  행수·결손일을 재는 read-only 스크립트. 대장님이 `--group feast`로 실행. CI 아님.

근거: CI는 "조회가 구조적으로 되는지", 스크립트는 "실제 데이터가 쌓였는지"를
각각 담당. 실 BQ를 CI에 넣지 않는 건 기존 feast 그룹 테스트 방침과 동일.

## 작업 분해 (순서) — seed 정리를 커버리지 측정보다 **먼저**

> ⚠️ 순서 함정: `generate_and_upload_dummy_data.py`가 채우는 더미 4테이블이
> 커버리지 측정 대상과 **동일**하고, 더미 row의 `event_timestamp`가
> `now(UTC) - 1h`(항상 "방금")로 찍힌다. seed가 최근 실행된 적 있으면 커버리지가
> "결손 없음"으로 **오판**한다 — Phase 0의 존재 이유가 무너지는 지점. 그래서
> 정리를 측정보다 먼저 둔다.

1. **TEMP_FEAST_BOOTSTRAP seed 실재 확인 + 정리**
   - 실 BQ 4테이블(user_static / user_dynamic / video_feature /
     user_category_similarity)에 더미 row(고정 패턴 `user_0001`, `video_00001`
     등)가 실제로 있는지 확인 후, 있으면 삭제.
   - seed 생성 스크립트 `generate_and_upload_dummy_data.py`의 삭제 여부 결정
     (독스트링이 "실 적재 확정되면 삭제" 명시).
   - 범위 구분: `verify_feature_retrieval.py`는 **online store(Redis) 검증**
     스크립트라 offline과 무관 — 같은 TEMP_FEAST_BOOTSTRAP 부채지만 별개 범위로
     명시하고, offline 정리와 혼동하지 않게 한다.
2. **실 BQ 커버리지 측정 스크립트** `scripts/verify_offline_coverage.py`
   - offline 4종 + `training_entity`의 기간·행수·결손일·학습 윈도우(최근 30일)
     커버리지 집계 read-only SQL. 결손일 목록 출력.
   - **더미 오염 방어(정리 누락 대비)**: 고정 패턴(`user_%`, `video_%` 등)을
     `WHERE ... NOT LIKE`로 제외하거나, 제외 못 하면 결과에 "seed 오염 가능성"
     경고를 출력해 실측 신뢰도를 지킨다.
3. **get_historical_features PIT 스모크** `tests/test_offline_retrieval_smoke_feast.py`
   - 로컬 파일 오프라인 스토어로 as-of 조회 실행, PIT가 이벤트 시각 이하 최신
     스냅샷을 고르는지 단언. 컬럼은 위 설계대로 `feature_definitions.py`에서 재사용.
   - **ttl 부재 stale fallback을 같은 테스트로 시연**(결손일에 더 오래된 스냅샷을
     조용히 붙임) — 4번 문서화의 실측 근거.
   - `ci.yml`의 feast 그룹 테스트 목록에 추가.
4. **ttl 부재 stale 위험 문서화** — `docs/guides/feature-store.md`에 전 FeatureView
   `ttl` 부재 → 결손일 stale fallback 위험과 방어 방향(#357 ttl 결정으로 연결) 명시.

## 검증

- feast 그룹: `uv sync --only-group feast` 환경에서 신규 스모크 테스트 pass.
- `ci.yml` feast 목록에 신규 테스트가 추가됐는지 확인.
- `ruff check autoresearch tests tools scripts` clean.
- 커버리지 스크립트는 실 BQ 필요 → 로컬은 문법/구조만, 실행은 대장님.

## 비범위

- FeatureService 정의·조회 전환 = #358.
- ttl 실제 도입 결정 = #357 (여기선 위험만 문서화).
- 실 BQ 커버리지 판정·백필 실행 = 대장님 실행 결과에 달림.

## 열린 질문

- `generate_and_upload_dummy_data.py`: 실 BQ에 더미가 없으면 스크립트만 삭제 vs
  당분간 보존? (독스트링은 "실 적재 확정 시 삭제" — 확정 여부는 step 1 실측에 달림)
- `verify_feature_retrieval.py`(online 잔재): #356(offline)에서 건드릴지 vs 별도
  이슈로 미룰지 — 범위가 online store라 이 이슈에선 문서 각주로만 다루는 쪽 후보.
