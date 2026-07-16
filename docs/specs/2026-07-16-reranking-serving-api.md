# YouTube Reranking Serving API

## 목표

후보 영상별 CTR 예측값을 반환하고, 높은 점수 순으로 정렬하는 FastAPI MVP를 제공한다.

## API 계약

- `GET /healthcheck`: 모델 로드 상태를 반환한다. 모델을 사용할 수 없으면 `503`을 반환한다.
- `POST /rerank`: `user_id`와 후보 목록을 받고, 각 후보의 사전 조립된 scalar feature로 CTR을 예측한다. 응답 `items`는 `ctr_score` 내림차순이다.
- `GET /metrics`: Prometheus 형식의 요청 수·지연 시간·모델 준비 상태를 노출한다.

`/rerank`의 후보는 `video_id`와 `features`를 가진다. `features`는 학습 artifact의 feature-column 목록을 모두 포함해야 하며, 벡터·리스트는 받지 않는다. MVP에서는 Feature Store 조회를 수행하지 않는다.

## 모델 artifact

- `RERANK_MODEL_SOURCE=local`: `RERANK_MODEL_PATH`의 joblib/pickle 모델과 `RERANK_FEATURE_COLUMNS_PATH`의 pickle feature 목록을 로드한다.
- `RERANK_MODEL_SOURCE=mlflow`: `MLFLOW_TRACKING_URI`와 `RERANK_MLFLOW_RUN_ID`를 사용해 `runs:/<run_id>/model/lgbm_model.joblib`, `runs:/<run_id>/features/feature_columns.pkl` artifact를 내려받아 로드한다.

현재 학습 파이프라인의 artifact 경로와 일치한다. MLflow registry alias를 통한 pyfunc 모델 로드는 학습 파이프라인이 MLflow model flavor를 기록하도록 확장될 때 별도 작업으로 다룬다.

## 컨테이너

`deploy/serving/Dockerfile`은 uv lockfile 기반으로 런타임 의존성을 설치한다. 로컬 모델은 이미지에 포함하지 않으며, 실행 환경에서 read-only volume 또는 artifact 다운로드로 제공한다.
