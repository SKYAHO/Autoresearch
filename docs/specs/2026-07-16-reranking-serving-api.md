# YouTube Reranking Serving API

## 목표

후보 영상별 CTR 예측값을 반환하고, 높은 점수 순으로 정렬하는 FastAPI MVP를 제공한다.

## API 계약

- `GET /healthcheck`: 모델 로드 상태를 반환한다. 모델을 사용할 수 없으면 `503`을 반환한다.
- `POST /rerank`: `user_id`와 `video_ids`를 받아 온라인 피처를 조립해 CTR을 예측한다. 응답 `items`는 `ctr_score` 내림차순이다.
- `GET /metrics`: Prometheus 형식의 요청 수·지연 시간·모델 준비 상태를 노출한다. 학습에 없던
  categorical 값이 NaN으로 강등되면(신규 카테고리 등장 등) `rerank_unseen_category_total{column=...}`
  카운터가 컬럼별로 증가하고 경고 로그가 남는다 — 조용한 학습-서빙 스큐를 감지해 재학습 신호로 쓴다.

`/rerank`은 외부 JSON에서 `user_id`와 `video_ids`만 받는다. `video_ids`는 1~200개의 비어 있지 않은 문자열이며 중복을 허용하지 않는다. 유효한 `video_ids`와 함께 들어온 legacy `candidates`를 포함해 선언되지 않은 필드는 `422`로 거부한다. 호출자는 모델 피처를 전달할 수 없으며, 구 계약의 하위 호환 이중 지원도 하지 않는다.

요청 예시:

```json
{
  "user_id": "user-1",
  "video_ids": ["video-1", "video-2"]
}
```

응답 항목은 요청 `user_id`를 반향하지 않는다. `model_id`는 예측에 사용한 불변 MLflow `run_id`이며, #216의 로컬 모델 계약에서는 `"local"`이다.

```json
{
  "items": [
    {"video_id": "video-2", "ctr_score": 0.71, "model_id": "run-123"},
    {"video_id": "video-1", "ctr_score": 0.42, "model_id": "run-123"}
  ]
}
```

## 온라인 피처 조립 계약

요청당 온라인 조회는 정확히 두 번의 Feast 배치 API 호출로 수행한다.

1. 입력 순서의 `(user_id, video_id)` 1~200행으로 `UserStaticView`, `UserDynamicView`, `VideoFeatureView`에서 직접 피처와 `preferred_category`를 읽는다.
2. 첫 조회의 고유 `(user_id, category_id)` 행으로 `UserCategorySimilarityView`의 `topic_similarity`를 읽고, 같은 category의 모든 영상에 다시 결합한다.

조회 결과는 entity key와 길이가 요청과 일치할 때만 결합한다. 모델에는 ID와 `preferred_category` 같은 조립 보조 컬럼을 전달하지 않는다. 모델 artifact의 피처 순서는 다음 15개와 정확히 같아야 한다.

| 순서 | 모델 입력 | 소스 또는 처리 |
| --- | --- | --- |
| 1 | `age_group` | UserStaticView |
| 2 | `occupation` | UserStaticView |
| 3 | `historical_category_affinity` | UserDynamicView |
| 4 | `recent_click_count_7d` | UserDynamicView |
| 5 | `recent_watch_time_7d` | UserDynamicView |
| 6 | `recent_like_count_7d` | UserDynamicView |
| 7 | `category_id` | VideoFeatureView |
| 8 | `duration_sec` | VideoFeatureView |
| 9 | `view_count` | VideoFeatureView |
| 10 | `like_ratio` | VideoFeatureView |
| 11 | `comment_ratio` | VideoFeatureView |
| 12 | `days_since_upload` | VideoFeatureView |
| 13 | `historical_category_match` | 기존 공용 계산 함수 |
| 14 | `preferred_category_match` | `preferred_category`를 보조 값으로 한 기존 공용 계산 함수 |
| 15 | `topic_similarity` | UserCategorySimilarityView |

| 결측 값 종류 | typed cold-start 기본값 |
| --- | --- |
| `age_group`, `occupation`, `historical_category_affinity`, `category_id` | `"unknown"` |
| `preferred_category` 보조 값 | `[]` |
| 최근 7일 count/watch-time, 영상 count/duration/age | `0` |
| `like_ratio`, `comment_ratio`, `topic_similarity` | `0.0` |
| 두 match 피처 | 위 기본값으로 공용 함수를 계산한 결과 `0` |

학습 categorical artifact에 `"unknown"`이 없으면 기존 Reranker가 값을 NaN으로 강등하고 `rerank_unseen_category_total`로 계측한다. 이 방식은 학습 의미가 다른 결측을 묵시적 숫자 `0`으로 바꾸지 않는다.

## 온라인 기록 범위

`/rerank`는 BigQuery에 온라인 요청을 동기 기록하지 않는다. #216의 일일 전체 순위 원장은 날짜 파티션 전체를 `WRITE_TRUNCATE`하므로 online append를 섞으면 배치 재실행에서 삭제되고, 현재 스키마에는 `request_id`, `source`, `served_at`도 없다. HTTP critical path에서의 BigQuery 호출은 지연 시간과 가용성에도 영향을 준다. 별도 온라인 감사 로그가 필요하면 append 전용 테이블과 비동기 sink를 설계하는 별도 이슈로 다룬다.

## 모델 artifact

- `RERANK_MODEL_SOURCE=local`: `RERANK_MODEL_PATH`의 joblib/pickle 모델,
  `RERANK_FEATURE_COLUMNS_PATH`의 pickle feature 목록,
  `RERANK_CATEGORICAL_COLUMNS_PATH`의 pickle 범주형 카테고리 dict를 로드한다.
- `RERANK_MODEL_SOURCE=mlflow`: `MLFLOW_TRACKING_URI`와 `RERANK_MLFLOW_RUN_ID`를 사용해
  `runs:/<run_id>/model/lgbm_model.joblib`, `runs:/<run_id>/features/feature_columns.pkl`,
  `runs:/<run_id>/features/categorical_columns.pkl` artifact를 내려받아 로드한다.

`categorical_columns.pkl`은 `dict[컬럼명, 카테고리 리스트]`이며 학습 시점
카테고리 값·순서를 보존한다. 서빙은 이 목록으로 `pd.Categorical`을 구성해
LightGBM category 코드 매핑을 학습과 동일하게 재현한다. 요청 feature 값의
타입은 학습 데이터와 동일해야 하며(예: 학습이 int였다면 int로 전송), 학습에
없던 카테고리 값은 결측(NaN)으로 처리된다. 이 아티팩트는 필수다 — 없는 기존
run은 재학습이 필요하다.

모델·feature·categorical artifact는 joblib/pickle로 역직렬화하며 이 과정에서
임의 코드가 실행될 수 있으므로, artifact는 신뢰된 출처(자체 학습 파이프라인
산출물 또는 신뢰된 MLflow tracking server)에서만 로드해야 한다.

현재 학습 파이프라인의 artifact 경로와 일치한다(경로 상수는
`src/serving/model_loader.py`와 `src/pipeline/train.py`가 계약으로 공유).
MLflow registry alias를 통한 pyfunc 모델 로드는 학습 파이프라인이 MLflow
model flavor를 기록하도록 확장될 때 별도 작업으로 다룬다.

## 컨테이너

`deploy/serving/Dockerfile`은 uv lockfile 기반으로 런타임 의존성을 설치한다. 로컬 모델은 이미지에 포함하지 않으며, 실행 환경에서 read-only volume 또는 artifact 다운로드로 제공한다.
