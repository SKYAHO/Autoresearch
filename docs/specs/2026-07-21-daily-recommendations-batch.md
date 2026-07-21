# 일일 추천 결과 BQ 적재 배치 (Daily Recommendations Batch)

## 목표

학습된 CTR 모델로 일일 트렌딩 후보 전체를 가상 유저 전원에 대해 채점하고,
유저별 전체 순위를 BigQuery `user_recommendations` 테이블에 매일 적재한다.
2026-07-21 수동 실험으로 검증된 경로(BQ 재료 → 피처 조립 → GKE MLflow 아티팩트
로드 → 채점 → 내림차순)를 저장소의 정식 배치 코드로 승격하는 작업이다.

## 배경

- 모델을 소비하는 상시 경로가 아직 없다. serving 서버는 요청-응답 계산기로
  완성되어 있으나 호출자가 없고, 정책 시뮬레이션 라운드(#195)는 비교 실험용
  일회 실행이다. 이 배치가 "모델의 채점 결과를 매일 남기는" 첫 정기 소비자가
  된다.
- 산출 테이블은 이후 두 소비자를 가진다: (1) 사후 분석(임의 K에 대한 순위
  분포·CTR@K), (2) 폐루프 일일 운영 시 model 정책의 점수 원장. **비교 실험과
  노출 선정은 이 배치의 책임이 아니다** — 비교의 공정성(두 정책 동일 k, 합동
  정규화)은 라운드 배치(#195)가, 노출 선정(Top-K + ε exploration)은
  `select_exposures`가 담당한다. 이 테이블의 rank는 순수 exploitation 순위다.

## 핵심 설계 결정

1. **저장 범위 = 유저당 전체 순위(후보 전수)** — Top-K만 저장하면 K를 바꾼
   사후 분석마다 재채점이 필요하다. 전체(현재 pool 기준 200위)를 저장하면
   임의 K 분석이 테이블 조회로 끝난다. 규모(6,983명 × 200 ≈ 일 140만 행)는
   BigQuery에서 사소하다.
2. **모델 지정 = `models:/ctr-model@champion` (registry alias)** — 모델 교체가
   env 수정·재배포 없이 "alias 이동"으로 끝난다. 이를 위해
   `src/serving/model_loader.py`에 registry 소스를 확장한다(서빙 spec
   `2026-07-16-reranking-serving-api.md`에 명시된 후속 과제의 해소).
   실행 계보는 alias가 가리키는 run_id를 해석해 산출 행에 기록한다.
3. **코드 위치 = `src/pipeline/`** — 이 배치는 `src.serving`(Reranker)과
   `src.features`(조립)를 소비하므로, `autoresearch → src` import 금지 규칙상
   `autoresearch/jobs/` 편입이 불가능하다. `simulate_policy_round`와 같은
   패턴으로 `src/pipeline/`에 두고 `python -m src.pipeline.daily_recommendations`
   실행 형태를 공개 배치 계약 문서에 등재한다.
4. **action log 단일 파티션 소비** — 일일 배치 하나가 독립된 30일 synthetic
   히스토리를 재생성하므로 파티션 간 UNION은 event_id 충돌·타임스탬프 겹침으로
   attribution과 point-in-time 집계를 오염시킨다(2026-07-21 실측: 10개 파티션
   합산 시 CTR 2%→17% 왜곡). 유저 행동 피처는 항상 **단일 dt 파티션**에서만
   조립한다.
5. **멱등 적재** — 같은 dt로 재실행하면 해당 날짜 행을 완전히 대체한다
   (파티션 덮어쓰기). 부분 실패 후 재실행이 중복을 만들지 않는다.

## 데이터 흐름

```
[모델]   models:/ctr-model@champion (GKE MLflow)
            └─ registry 소스 로더 → 아티팩트 3종 → Reranker (서빙과 동일 코드)

[재료]   BQ feast_offline_store (단일 프로젝트·데이터셋, 테이블명은 환경변수로 재정의 가능)
            ├─ data_lake_youtube_trending_kr  WHERE dt = <candidate_dt>   → 후보 pool
            ├─ asset_virtual_user_vu_1000                                  → 유저 전원 + personas 어댑터
            └─ data_lake_action_log           WHERE dt = <events_dt>       → long → derive_wide_events → 행동 피처 원천

[채점]   유저별: build_pool_feature_frame(학습과 동일 assembly 함수)
            → Reranker.rerank → 후보 전수 내림차순

[적재]   user_recommendations (dt 파티션 덮어쓰기)
```

## 컴포넌트

### 1. registry 소스 확장 — `src/serving/model_loader.py` (수정)

- `ModelSource`에 `REGISTRY = "registry"` 추가.
- `RegistryModelSettings(tracking_uri, model_name, alias)` 신설. 환경변수:
  `MLFLOW_TRACKING_URI`, `RERANK_REGISTRY_MODEL_NAME`(기본 `ctr-model`),
  `RERANK_REGISTRY_ALIAS`(기본 `champion`).
- 로드 절차: `MlflowClient.get_model_version_by_alias(name, alias)`로 버전을
  해석해 **run_id를 얻고, 이후는 기존 `load_mlflow_model`과 동일한 run 아티팩트
  다운로드 경로를 재사용**한다(아티팩트 경로 상수 계약 유지). 해석된
  run_id·버전은 호출자가 계보 기록에 쓸 수 있도록 반환 구조에 포함한다 —
  공개 함수 `load_reranker_with_lineage(settings) -> ResolvedModel`
  (`ResolvedModel(reranker, run_id, model_version | None)`; local/mlflow
  소스에서는 version이 None). 기존 `load_reranker`는 시그니처 불변.
- 기존 `local`/`mlflow` 소스의 동작·시그니처는 변경하지 않는다(서빙 하위 호환).

### 2. personas 어댑터 정식화 — `src/pipeline/virtual_user_adapter.py` (신규)

BQ 가상 유저 테이블을 학습·조립 계약 형태(`uuid, age, occupation,
hobbies_and_interests_list(JSON), hobbies_and_interests(텍스트)`)의 DataFrame으로
변환한다.

- 키워드 컬럼(`hobby/interest/lifestyle_keywords`)의 **BQ Arrow 중첩 구조
  (`{'list': [{'element': str}, ...]}`)와 평평한 배열, None을 모두 처리**한다
  (2026-07-21 실측 결함: 중첩 미처리 시 전 키워드가 문자열 `"list"`로 붕괴,
  topic_similarity 피처 무의미화 → v2 모델 결함의 원인).
- 순수 함수(`to_personas_frame(vu_df) -> pd.DataFrame`)로 두어 BQ 없이 단위
  테스트한다.

### 3. 배치 진입점 — `src/pipeline/daily_recommendations.py` (신규)

`main()` 흐름:

1. registry 소스로 Reranker 로드 (fail-fast — 모델 없이는 라운드 무효)
2. BQ 로드: 후보(candidate_dt 파티션), 가상 유저 전원 → 어댑터, action log
   (events_dt 단일 파티션) → `derive_wide_events`
3. 유저별 `build_pool_feature_frame` → `rerank` → 전수 순위. 유저 단위
   격리: 개별 유저 실패는 skip 계수(quarantine 로그), 실패 비율이 임계치
   (기본 10%) 초과 시 전체 실패
4. 적재: 결과 행 조립 → `user_recommendations`의 해당 dt 파티션 덮어쓰기
5. 요약 리포트 stdout(JSON): 유저 수, skip 수, 적재 행 수, model run_id/버전

CLI 인자(공개 계약): `--candidate-dt`(기본: 후보 테이블 MAX(dt)),
`--events-dt`(기본: action log MAX(dt)), `--max-users`(기본 무제한),
`--output-table`(기본 `user_recommendations`), `--dry-run`(적재 생략, 요약만).
파티션·테이블·프로젝트는 기존 `CTR_TRAINING_BQ_*` 환경변수 체계를 재사용한다.

### 4. 출력 테이블 계약 — `feast_offline_store.user_recommendations`

| 컬럼 | 타입 | 의미 |
| --- | --- | --- |
| `dt` | DATE (파티션) | 추천 기준일 = candidate_dt |
| `user_id` | STRING | 가상 유저 ID |
| `video_id` | STRING | 후보 영상 |
| `rank` | INTEGER | 1-base, ctr_score 내림차순 (동점은 video_id 오름차순으로 결정론 보장) |
| `ctr_score` | FLOAT | 모델 점수 — **보정된 확률이 아닌 순위용 상대 점수** |
| `model_run_id` | STRING | 계보: 채점에 쓴 MLflow run |
| `model_version` | STRING | 계보: Registry 버전 번호 |
| `events_dt` | DATE | 유저 피처를 조립한 action log 파티션 |
| `generated_at` | TIMESTAMP | 적재 시각 |

테이블이 없으면 배치가 위 스키마로 생성한다. 멱등성: **파티션 데코레이터
(`user_recommendations$YYYYMMDD`) + WRITE_TRUNCATE**로 해당 날짜 파티션을
원자적으로 대체한다. "같은 dt 재실행 시 중복 0"을 테스트로 고정한다.

### 5. 공개 배치 계약 등재 — `docs/specs/2026-07-13-public-batch-execution-contract.md` (수정)

`python -m src.pipeline.daily_recommendations` 명령과 인자·환경변수를 등재한다.
스케줄(빈도·재시도·타임아웃)은 `Autoresearch-airflow` 소유로 명시한다.

## 에러 처리

- **fail-fast**: registry alias 해석 실패, 아티팩트 로드 실패, 후보/유저/이벤트
  테이블 조회 실패, 후보 0건 — 시작 즉시 중단 (부분 적재 없음: 적재는 전
  유저 채점 완료 후 1회).
- **유저 단위 격리**: 개별 유저의 어댑터·조립·채점 실패는 skip하고 user_id를
  로그로 남긴다. skip 비율 > `--max-skip-ratio`(기본 0.1)이면 적재 없이 전체
  실패 — 대량 실패를 조용히 절반짜리 추천으로 만들지 않는다.
- **콜드스타트는 정상 경로**: 이벤트 이력이 없는 유저는 실패가 아니라
  affinity="unknown"·집계 0으로 채점된다(학습 경로와 동일 의미).

## 테스트

`tests/` 플랫 구조.

1. **어댑터 단위**: 중첩 구조/평평한 배열/None/빈 배열 각각에서 키워드가
   보존되는지 — v2 결함의 회귀 테스트.
2. **registry 로더 단위**: stub `MlflowClient`로 alias → run_id 해석과 기존
   다운로드 경로 재사용을 검증. 기존 local/mlflow 소스 테스트는 무수정 통과.
3. **rank 결정론**: 동점 ctr_score에서 video_id 오름차순 tie-break 고정.
4. **멱등성**: 적재 로직을 fake BQ client(또는 로컬 검증 가능한 추상화)로
   같은 dt 2회 실행 → 행 수 불변.
5. **e2e 스모크**: fake 재료 + stub Reranker로 main() 실행 → 요약 리포트와
   적재 호출 형태 검증 (실 BQ·MLflow 미접속).

## 비범위 (Non-goals)

- 노출 선정(Top-K + ε)과 LLM 판정 — 라운드 배치(#195) 소관.
- serving HTTP 경유 — 배치는 Reranker를 직접 로드한다(방안 A 관례).
- Feast materialize·online store — 별도 트랙.
- Airflow DAG 작성 — `Autoresearch-airflow` 소유. 이 저장소는 공개 CLI까지만.

## 운영 노트

- champion alias가 결함 세대를 가리키면 배치도 그대로 오염된다 — alias 이동은
  Registry 태그(`deprecated` 등) 확인 후 수행하는 운영 관례를 전제한다.
- 규모: 6,983 × 200 = 일 ~140만 행, LightGBM 채점 수 분 내. pool이 커져
  전수 채점이 부담이 되는 시점에는 후보 생성(retrieval) 단계 도입이 별도
  과제로 필요하다.
