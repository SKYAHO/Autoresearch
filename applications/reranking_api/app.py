"""온라인 피처 기반 CTR 리랭킹 HTTP 서빙 경계.

[파이프라인] 일일 추천 배치와 action log 재학습 사이의 실시간 요청 경로에서,
학습된 CTR 모델과 온라인 피처를 사용해 요청 영상의 리랭킹 결과를 제공한다.

[기능] FastAPI 수명주기 의존성 초기화와 readiness 판정, healthcheck·rerank·metrics
HTTP 계약, 요청 순서 응답과 고정 카디널리티의 단계별 서빙 메트릭을 담당한다.

[비책임] 모델 아티팩트 해석(applications/reranking_api/model_loader.py), 온라인 피처 조회·조립
(applications/reranking_api/online_features.py), CTR 예측 구현(applications/reranking_api/service.py), Airflow 배포
및 스케줄링(Autoresearch-airflow 저장소).
"""

from __future__ import annotations

import logging
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager

from autoresearch.logging_json import setup_json_logging
from fastapi import FastAPI, HTTPException, status
from fastapi.responses import Response
from prometheus_client import CONTENT_TYPE_LATEST, Counter, Gauge, Histogram, generate_latest

from autoresearch.feature_engineering.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    FeatureContractError,
    MODEL_FEATURE_COLUMNS,
)
from applications.reranking_api.feast_reader import load_feast_online_feature_reader
from applications.reranking_api.model_loader import (
    ResolvedModel,
    load_model_settings_from_environment,
    load_reranker_with_lineage,
)
from applications.reranking_api.online_features import (
    FeatureRetrievalError,
    ServingFeatureBuilder,
)
from applications.reranking_api.schemas import (
    HealthcheckResponse,
    RerankRequest,
    RerankResponse,
    RerankResponseItem,
)
from applications.reranking_api.service import PredictionError

# uvicorn이 자체 로깅을 구성한 뒤 이 모듈을 import하므로, 여기서 JSON
# stdout으로 재구성해야 access 로그까지 구조화된다 (#352, 계약은
# autoresearch.logging_json docstring 참조).
setup_json_logging()

# 아래 메트릭들은 모듈 전역 레지스트리에 등록된다 — uvicorn을 --workers>1로 늘리면
# 워커별로 값이 분리되어 /metrics가 워커마다 다르게 보인다. 스케일업 시
# PROMETHEUS_MULTIPROC_DIR 기반 멀티프로세스 설정이 필요하다.
RERANK_REQUESTS = Counter("rerank_requests", "Number of reranking requests.")
RERANK_VIDEO_IDS = Histogram(
    "rerank_video_ids",
    "Video ID count per reranking request.",
    buckets=(1, 2, 5, 10, 20, 50, 100, 200, 500),
)
RERANK_CANDIDATES = Histogram(
    "rerank_candidates",
    "DEPRECATED: Candidate count per reranking request; migrate to rerank_video_ids.",
    buckets=(1, 2, 5, 10, 20, 50, 100, 200, 500),
)
RERANK_DURATION = Histogram("rerank_duration_seconds", "Reranking request duration.")
RERANK_PHASE_DURATION = Histogram(
    "rerank_phase_duration_seconds",
    "Duration of fixed reranking request phases.",
    ["phase"],
)
RERANK_OUTCOMES = Counter(
    "rerank_outcomes",
    "Reranking request outcomes after request validation.",
    ["outcome"],
)
RERANK_IN_FLIGHT = Gauge(
    "rerank_in_flight",
    "Reranking requests currently executing after request validation.",
)
RERANK_MODEL_READY = Gauge(
    "rerank_model_ready",
    "Whether the model, online feature store, and feature contract are ready.",
)
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
        incompatible_categorical_columns = tuple(
            column
            for column in CATEGORICAL_FEATURE_COLUMNS
            if column in active_model.reranker.categorical_categories
            and not all(
                isinstance(value, str)
                for value in active_model.reranker.categorical_categories[column]
            )
        )
        if incompatible_categorical_columns:
            return "Model categorical values do not match the serving feature types."
        return None

    @asynccontextmanager
    async def lifespan(_: FastAPI) -> AsyncIterator[None]:
        nonlocal active_feature_builder, active_model
        if load_from_environment:
            initialization_phase = "model"
            try:
                settings = load_model_settings_from_environment()
                active_model = load_reranker_with_lineage(settings)
                initialization_phase = "feature_store"
                reader = load_feast_online_feature_reader(
                    os.getenv("RERANK_FEATURE_REPO_PATH", "feature_repo")
                )
                active_feature_builder = ServingFeatureBuilder(reader=reader)
            except Exception as error:  # noqa: BLE001 - startup boundary must remain health-queryable.
                # 설정·인증 실패는 연결 문자열이나 토큰을 예외 문자열에 포함할 수 있으므로,
                # 이 경계에서는 안전한 phase/error type만 기록한다.
                logger.error(
                    "Reranking runtime initialization failed: phase=%s error_type=%s",
                    initialization_phase,
                    type(error).__name__,
                )
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
        RERANK_IN_FLIGHT.inc()
        try:
            with RERANK_DURATION.time():
                detail = unavailable_detail()
                if detail is not None:
                    RERANK_OUTCOMES.labels(outcome="unavailable").inc()
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=detail,
                    )

                RERANK_REQUESTS.inc()
                video_id_count = len(request.video_ids)
                RERANK_VIDEO_IDS.observe(video_id_count)
                RERANK_CANDIDATES.observe(video_id_count)
                try:
                    feature_build = active_feature_builder.build_with_timings(
                        user_id=request.user_id,
                        video_ids=request.video_ids,
                        feature_columns=active_model.reranker.feature_columns,
                    )
                    RERANK_PHASE_DURATION.labels(phase="feature_read_first").observe(
                        feature_build.timings.first_read_seconds
                    )
                    RERANK_PHASE_DURATION.labels(phase="feature_read_second").observe(
                        feature_build.timings.second_read_seconds
                    )
                    RERANK_PHASE_DURATION.labels(phase="feature_assemble").observe(
                        feature_build.timings.assemble_seconds
                    )

                    with RERANK_PHASE_DURATION.labels(phase="model_predict").time():
                        outcome = active_model.reranker.rerank_with_diagnostics(
                            feature_build.candidates
                        )
                        requested_video_ids = set(request.video_ids)
                        outcome_video_ids = [item.video_id for item in outcome.items]
                        if (
                            len(outcome_video_ids) != len(requested_video_ids)
                            or set(outcome_video_ids) != requested_video_ids
                        ):
                            raise PredictionError(
                                reason="Reranker returned unexpected video IDs."
                            )
                # Pydantic이 호출자 요청 형태를 422로 검증한다. 여기의 오류는 그 이후
                # 모델·Feast 경계에서 발견된 서버측 계약/조회 장애이므로 503으로 표면화한다.
                except (FeatureContractError, FeatureRetrievalError) as error:
                    RERANK_OUTCOMES.labels(outcome="feature_error").inc()
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail=str(error),
                    ) from error
                except PredictionError as error:
                    RERANK_OUTCOMES.labels(outcome="prediction_error").inc()
                    raise HTTPException(
                        status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                        detail="Reranking model returned an invalid prediction.",
                    ) from error

                with RERANK_PHASE_DURATION.labels(phase="response_build").time():
                    # 학습에 없던 카테고리 값은 조용히 NaN이 되어 예측을 오염시키므로,
                    # 계측·로깅해 감지한다.
                    for column, values in outcome.unseen_categories.items():
                        RERANK_UNSEEN_CATEGORY.labels(column=column).inc(len(values))
                        logger.warning(
                            "Unseen categorical values coerced to NaN (retraining may be needed): "
                            "column=%s count=%d sample=%s",
                            column,
                            len(values),
                            sorted({str(value) for value in values})[:10],
                        )
                    scores_by_video_id = {
                        item.video_id: item.ctr_score for item in outcome.items
                    }
                    response = RerankResponse(
                        items=[
                            RerankResponseItem(
                                video_id=video_id,
                                ctr_score=scores_by_video_id[video_id],
                                model_id=active_model.run_id,
                            )
                            for video_id in request.video_ids
                        ]
                    )
                RERANK_OUTCOMES.labels(outcome="success").inc()
                return response
        finally:
            RERANK_IN_FLIGHT.dec()

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus 스크레이프용 메트릭을 텍스트 포맷으로 노출한다."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
