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
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from time import perf_counter

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

# 과부하 거절 시 호출부에 제시하는 재시도 대기 초. 호출부가 고정 백오프 대신
# 서버가 제시한 값을 쓸 수 있다.
RETRY_AFTER_SECONDS: int = 1

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


def _positive_environment_value(name: str) -> float | None:
    """양수 설정값을 읽는다. 미설정·빈 값이면 None(기능 비활성)이다.

    기본값으로 조용히 거동을 바꾸지 않는다 — 운영자가 명시적으로 켜야 한다.
    """
    raw = os.getenv(name, "").strip()
    if not raw:
        return None
    try:
        value = float(raw)
    except ValueError as error:
        raise ValueError(f"{name} must be a positive number; received {raw!r}") from error
    if not value > 0:
        raise ValueError(f"{name} must be a positive number; received {raw!r}")
    return value


class _ConcurrencyGate:
    """동시 실행 수를 세어 상한 초과 요청을 즉시 거절한다.

    동기 핸들러가 anyio 스레드풀의 여러 스레드에서 돌므로 판정과 증감이 원자적이어야
    한다. 세마포어가 아니라 Lock + 카운터를 쓰는 이유는, 세마포어는 자리가 없을 때
    **대기**하는 것이 기본이라 대기열을 없애려는 목적과 반대이기 때문이다. 여기서는
    자리가 없으면 기다리지 않고 즉시 거절한다.
    """

    def __init__(self, limit: int | None) -> None:
        self._limit = limit
        self._lock = threading.Lock()
        self._active = 0

    def try_acquire(self) -> bool:
        if self._limit is None:
            return True
        with self._lock:
            if self._active >= self._limit:
                return False
            self._active += 1
            return True

    def release(self) -> None:
        if self._limit is None:
            return
        with self._lock:
            self._active -= 1


def create_app(
    resolved_model: ResolvedModel | None = None,
    feature_builder: ServingFeatureBuilder | None = None,
) -> FastAPI:
    """주입된 모델 계보와 온라인 피처 조립기로 FastAPI 앱을 조립한다."""
    active_model = resolved_model
    active_feature_builder = feature_builder
    load_from_environment = resolved_model is None and feature_builder is None

    max_concurrency = _positive_environment_value("RERANK_MAX_CONCURRENCY")
    request_timeout_seconds = _positive_environment_value("RERANK_REQUEST_TIMEOUT_SECONDS")
    concurrency_gate = _ConcurrencyGate(
        int(max_concurrency) if max_concurrency is not None else None
    )

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
        # 상한 판정은 readiness보다 앞이고 피처 조회·예측보다 앞이다. 거절 경로가
        # 비싸면 과부하에서 차단이 오히려 부하를 키운다.
        if not concurrency_gate.try_acquire():
            RERANK_OUTCOMES.labels(outcome="overloaded").inc()
            raise HTTPException(
                status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                detail="Reranking is over its concurrency limit.",
                headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
            )
        started = perf_counter()
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
                # 상한 안에 들어온 요청이 의존성 지연으로 오래 붙잡혀 있으면 그만큼
                # 새 요청이 거절된다. 예산을 넘긴 응답은 호출부 입장에서 이미 쓸모가
                # 없으므로, 성공으로 세지 않고 끊어 자리를 돌려준다.
                if (
                    request_timeout_seconds is not None
                    and perf_counter() - started > request_timeout_seconds
                ):
                    RERANK_OUTCOMES.labels(outcome="timeout").inc()
                    raise HTTPException(
                        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
                        detail="Reranking exceeded its request time budget.",
                        headers={"Retry-After": str(RETRY_AFTER_SECONDS)},
                    )
                RERANK_OUTCOMES.labels(outcome="success").inc()
                return response
        finally:
            RERANK_IN_FLIGHT.dec()
            concurrency_gate.release()

    @app.get("/metrics", include_in_schema=False)
    def metrics() -> Response:
        """Prometheus 스크레이프용 메트릭을 텍스트 포맷으로 노출한다."""
        return Response(content=generate_latest(), media_type=CONTENT_TYPE_LATEST)

    return app


app = create_app()
