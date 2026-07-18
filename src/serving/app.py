from __future__ import annotations

import logging
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
# 학습에 없던 categorical 값이 NaN으로 조용히 강등된 횟수(컬럼별). 신규 카테고리 등장 =
# 학습-서빙 스큐 신호이며, 재학습 트리거로 쓴다. 라벨은 컬럼명만 사용해 카디널리티를 제한한다.
RERANK_UNSEEN_CATEGORY = Counter(
    "rerank_unseen_category",
    "Count of categorical values coerced to NaN because they were unseen at training time.",
    ["column"],
)

logger = logging.getLogger(__name__)


def create_app(reranker: Reranker | None = None) -> FastAPI:
    """FastAPI 앱을 조립한다. reranker를 주입하면 그대로 쓰고, 없으면 시작 시 환경변수로 로드한다."""
    active_reranker = reranker
    model_load_error: ModelConfigurationError | ModelArtifactError | None = None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        # 앱 시작 시 모델을 1회 로드하고 준비 상태 게이지를 갱신, 종료 시 0으로 되돌린다.
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
        """모델 준비 여부를 확인한다. 미준비면 503(로드 실패 사유 포함)을 반환한다."""
        if active_reranker is None:
            detail = "Reranking model is unavailable."
            if model_load_error is not None:
                detail = str(model_load_error)
            raise HTTPException(status_code=status.HTTP_503_SERVICE_UNAVAILABLE, detail=detail)
        return HealthcheckResponse(status="ok")

    @app.post("/rerank", response_model=RerankResponse)
    def rerank(request: RerankRequest) -> RerankResponse:
        """후보 영상을 CTR 예측으로 재정렬한다. 메트릭을 기록하고 도메인 예외를 HTTP 상태로 매핑한다."""
        if active_reranker is None:
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Reranking model is unavailable.",
            )

        RERANK_REQUESTS.inc()
        RERANK_CANDIDATES.observe(len(request.candidates))
        with RERANK_DURATION.time():
            try:
                outcome = active_reranker.rerank_with_diagnostics(request.candidates)
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
        return RerankResponse(items=outcome.items)

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus 스크레이프용 메트릭을 텍스트 포맷으로 노출한다."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
