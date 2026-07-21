from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from src.serving.feast_reader import load_feast_online_feature_reader
from src.serving.model_loader import (
    ResolvedModel,
    load_model_settings_from_environment,
    load_reranker_with_lineage,
)
from src.serving.online_features import (
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
    FeatureRetrievalError,
    ServingFeatureBuilder,
)
from src.serving.schemas import (
    HealthcheckResponse,
    RerankRequest,
    RerankResponse,
    RerankResponseItem,
)
from src.serving.service import PredictionError

# 아래 메트릭들은 모듈 전역 레지스트리에 등록된다 — uvicorn을 --workers>1로 늘리면
# 워커별로 값이 분리되어 /metrics가 워커마다 다르게 보인다. 스케일업 시
# PROMETHEUS_MULTIPROC_DIR 기반 멀티프로세스 설정이 필요하다.
RERANK_REQUESTS = Counter("rerank_requests", "Number of reranking requests.")
RERANK_VIDEO_IDS = Histogram(
    "rerank_video_ids",
    "Video ID count per reranking request.",
    buckets=(1, 2, 5, 10, 20, 50, 100, 200, 500),
)
RERANK_DURATION = Histogram("rerank_duration_seconds", "Reranking request duration.")
RERANK_MODEL_READY = Gauge("rerank_model_ready", "Whether a reranking model is ready.")
# 학습에 없던 categorical 값이 NaN으로 조용히 강등된 횟수(컬럼별). 신규 카테고리 등장 =
# 학습-서빙 스큐 신호이며, 재학습 트리거로 쓴다. 라벨은 컬럼명만 사용해 카디널리티를 제한한다.
RERANK_UNSEEN_CATEGORY = Counter(
    "rerank_unseen_category",
    "Count of categorical values coerced to NaN because they were unseen at training time.",
    ["column"],
)

logger = logging.getLogger(__name__)


def create_app(
    resolved_model: ResolvedModel | None = None,
    feature_builder: ServingFeatureBuilder | None = None,
) -> FastAPI:
    """주입된 모델 계보와 온라인 피처 조립기로 FastAPI 앱을 조립한다."""
    active_model = resolved_model
    active_feature_builder = feature_builder
    load_from_environment = resolved_model is None and feature_builder is None

    def unavailable_detail() -> str | None:
        if active_model is None:
            return "Reranking model is unavailable."
        if active_feature_builder is None:
            return "Online feature store is unavailable."
        if active_model.reranker.feature_columns != MODEL_FEATURE_COLUMNS:
            return "Model feature columns do not match the serving contract."
        return None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal active_feature_builder, active_model
        if load_from_environment:
            try:
                settings = load_model_settings_from_environment()
                active_model = load_reranker_with_lineage(settings)
                reader = load_feast_online_feature_reader(
                    os.getenv("RERANK_FEATURE_REPO_PATH", "feature_repo")
                )
                active_feature_builder = ServingFeatureBuilder(reader=reader)
            except Exception:  # noqa: BLE001 - startup boundary must remain health-queryable.
                logger.error("Reranking runtime initialization failed.")
        RERANK_MODEL_READY.set(1 if unavailable_detail() is None else 0)
        yield
        RERANK_MODEL_READY.set(0)

    app = FastAPI(title="YouTube Reranking Serving API", lifespan=lifespan)

    @app.get("/healthcheck", response_model=HealthcheckResponse)
    def healthcheck() -> HealthcheckResponse:
        detail = unavailable_detail()
        if detail is not None:
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
        return HealthcheckResponse(status="ok")

    @app.post("/rerank", response_model=RerankResponse)
    def rerank(request: RerankRequest) -> RerankResponse:
        detail = unavailable_detail()
        if detail is not None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail=detail,
            )

        RERANK_REQUESTS.inc()
        RERANK_VIDEO_IDS.observe(len(request.video_ids))
        with RERANK_DURATION.time():
            try:
                candidates = active_feature_builder.build(
                    user_id=request.user_id,
                    video_ids=request.video_ids,
                    feature_columns=active_model.reranker.feature_columns,
                )
                outcome = active_model.reranker.rerank_with_diagnostics(candidates)
            except (FeatureContractError, FeatureRetrievalError) as error:
                raise HTTPException(
                    status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                    detail=str(error),
                ) from error
            except PredictionError as error:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Reranking model returned an invalid prediction.",
                ) from error

        # 학습에 없던 카테고리 값은 조용히 NaN이 되어 예측을 오염시키므로, 계측·로깅해 감지한다.
        for column, values in outcome.unseen_categories.items():
            RERANK_UNSEEN_CATEGORY.labels(column=column).inc(len(values))
            logger.warning(
                "Unseen categorical values coerced to NaN (retraining may be needed): "
                "column=%s count=%d sample=%s",
                column,
                len(values),
                sorted({str(value) for value in values})[:10],
            )
        scores_by_video_id = {item.video_id: item.ctr_score for item in outcome.items}
        return RerankResponse(
            items=[
                RerankResponseItem(
                    video_id=video_id,
                    ctr_score=scores_by_video_id[video_id],
                    model_id=active_model.run_id,
                )
                for video_id in request.video_ids
            ]
        )

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus 스크레이프용 메트릭을 텍스트 포맷으로 노출한다."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
