from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from src.serving.model_loader import (
    ModelArtifactError,
    ModelConfigurationError,
    load_model_settings_from_environment,
    load_reranker,
)
from src.serving.schemas import HealthcheckResponse, RerankRequest, RerankResponse
from src.serving.service import MissingFeatureColumnsError, PredictionError, Reranker

RERANK_REQUESTS = Counter("rerank_requests", "Number of reranking requests.")
RERANK_CANDIDATES = Histogram(
    "rerank_candidates",
    "Candidate count per reranking request.",
    buckets=(1, 2, 5, 10, 20, 50, 100, 200, 500),
)
RERANK_DURATION = Histogram("rerank_duration_seconds", "Reranking request duration.")
RERANK_MODEL_READY = Gauge("rerank_model_ready", "Whether a reranking model is ready.")


def create_app(reranker: Reranker | None = None) -> FastAPI:
    active_reranker = reranker
    model_load_error: ModelConfigurationError | ModelArtifactError | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal active_reranker, model_load_error
        if active_reranker is None:
            try:
                active_reranker = load_reranker(load_model_settings_from_environment())
            except (ModelConfigurationError, ModelArtifactError) as error:
                model_load_error = error
        RERANK_MODEL_READY.set(1 if active_reranker is not None else 0)
        yield
        RERANK_MODEL_READY.set(0)

    app = FastAPI(title="YouTube Reranking Serving API", lifespan=lifespan)

    @app.get("/healthcheck", response_model=HealthcheckResponse)
    def healthcheck() -> HealthcheckResponse:
        if active_reranker is None:
            detail = "Reranking model is unavailable."
            if model_load_error is not None:
                detail = str(model_load_error)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
        return HealthcheckResponse(status="ok")

    @app.post("/rerank", response_model=RerankResponse)
    def rerank(request: RerankRequest) -> RerankResponse:
        if active_reranker is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Reranking model is unavailable.",
            )

        RERANK_REQUESTS.inc()
        RERANK_CANDIDATES.observe(len(request.candidates))
        with RERANK_DURATION.time():
            try:
                items = active_reranker.rerank(request.candidates)
            except MissingFeatureColumnsError as error:
                raise HTTPException(
                    status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                    detail=str(error),
                ) from error
            except PredictionError as error:
                raise HTTPException(
                    status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                    detail="Reranking model returned an invalid prediction.",
                ) from error
        return RerankResponse(items=items)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
