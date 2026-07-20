# YouTube Reranking Serving API

## 목표

후보 영상별 CTR 예측값을 반환하고, 높은 점수 순으로 정렬하는 FastAPI MVP를 제공한다.

## API 계약

- `GET /healthcheck`: 모델 로드 상태를 반환한다. 모델을 사용할 수 없으면 `503`을 반환한다.
- `POST /rerank`: `user_id`와 후보 목록을 받고, 각 후보의 사전 조립된 scalar feature로 CTR을 예측한다. 응답 `items`는 `ctr_score` 내림차순이다.
- `GET /metrics`: Prometheus 형식의 요청 수·지연 시간·모델 준비 상태를 노출한다. 학습에 없던
  categorical 값이 NaN으로 강등되면(신규 카테고리 등장 등) `rerank_unseen_category_total{column=...}`
  카운터가 컬럼별로 증가하고 경고 로그가 남는다 — 조용한 학습-서빙 스큐를 감지해 재학습 신호로 쓴다.

`/rerank`의 후보는 `video_id`와 `features`를 가진다. `features`는 학습 artifact의 feature-column 목록을 모두 포함해야 하며, 벡터·리스트는 받지 않는다. MVP에서는 Feature Store 조회를 수행하지 않는다. 학습 카테고리에 없는 값이 오면 요청은 실패하지 않고 해당 값을 NaN(결측)으로 처리하되, 위 `rerank_unseen_category_total`로 계측한다. 이 강등에는 **타입 불일치**도 포함된다 — 예: 학습 카테고리가 `int (10, 20, 30)`인데 요청이 `str "10"`으로 오면 매칭에 실패해 NaN이 된다. MVP는 요청 값을 학습 카테고리 타입으로 정규화(coerce)하지 않으며, 이 조용한 왜곡은 예방이 아니라 위 메트릭으로 **감지**하는 것을 계약으로 한다(정규화는 후속 과제).

이 감지가 HTTP 경로에서 성립하는 것은 `FeatureValue = str | int | float | bool`이 pydantic v2 **smart union**으로 검증되어, JSON 값의 타입이 유니온 멤버와 정확히 일치하면 그대로 보존되기 때문이다(`"10"`은 `str`로 남고 `int`로 coerce되지 않는다). 이 유니온을 좁히거나 `union_mode='left_to_right'` 같은 순차 검증으로 바꾸면 요청 값이 조용히 변환되어 불일치가 감지되지 않고 메트릭이 죽은 코드가 된다.

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
