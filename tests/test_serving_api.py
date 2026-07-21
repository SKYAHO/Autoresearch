from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass, field

import numpy as np
import pandas as pd
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from src.serving.app import create_app
from src.serving.model_loader import ResolvedModel
from src.serving.online_features import (
    MODEL_FEATURE_COLUMNS,
    FeatureRetrievalError,
)
from src.serving.schemas import CandidateVideo
from src.serving.service import Reranker


class RecordingModel:
    def __init__(self) -> None:
        self.received: pd.DataFrame | None = None

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        self.received = features
        scores = features["view_count"].to_numpy(dtype=float) / 100.0
        return np.column_stack((1.0 - scores, scores))


class RaisingModel:
    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        raise RuntimeError("prediction failed")


@dataclass
class FakeFeatureBuilder:
    calls: list[tuple[str, tuple[str, ...], tuple[str, ...]]] = field(default_factory=list)

    def build(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> list[CandidateVideo]:
        self.calls.append((user_id, tuple(video_ids), tuple(feature_columns)))
        return [
            CandidateVideo(
                video_id=video_id,
                features={
                    column: float(index + 1) * 10.0
                    for column in MODEL_FEATURE_COLUMNS
                },
            )
            for index, video_id in enumerate(video_ids)
        ]


@dataclass
class FailingFeatureBuilder:
    def build(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> list[CandidateVideo]:
        raise FeatureRetrievalError(reason="online feature store is unavailable")


def _resolved_model(model: RecordingModel | None = None) -> ResolvedModel:
    active_model = model or RecordingModel()
    return ResolvedModel(
        reranker=Reranker(
            model=active_model,
            feature_columns=MODEL_FEATURE_COLUMNS,
            categorical_categories={},
        ),
        run_id="run-123",
        model_version="7",
    )


def test_rerank_builds_features_from_user_id_and_video_ids() -> None:
    model = RecordingModel()
    builder = FakeFeatureBuilder()
    app = create_app(resolved_model=_resolved_model(model), feature_builder=builder)

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-1", "video-2"]},
        )

    assert response.status_code == 200
    assert builder.calls == [("user-1", ("video-1", "video-2"), MODEL_FEATURE_COLUMNS)]
    assert model.received is not None
    assert tuple(model.received.columns) == MODEL_FEATURE_COLUMNS
    assert "user_id" not in model.received
    assert "video_id" not in model.received
    assert "preferred_category" not in model.received


def test_rerank_returns_scores_with_model_run_id() -> None:
    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-low", "video-high"]},
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"video_id": "video-high", "ctr_score": 0.2, "model_id": "run-123"},
            {"video_id": "video-low", "ctr_score": 0.1, "model_id": "run-123"},
        ]
    }


def test_rerank_does_not_accept_caller_supplied_features() -> None:
    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={
                "user_id": "user-1",
                "video_ids": ["video-1"],
                "candidates": [{"video_id": "video-1", "features": {}}],
            },
        )

    assert response.status_code == 422


def test_rerank_maps_feature_store_failure_to_503() -> None:
    app = create_app(
        resolved_model=_resolved_model(), feature_builder=FailingFeatureBuilder()
    )

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-1"]},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "online feature store is unavailable"}


def test_healthcheck_requires_both_model_and_feature_store() -> None:
    incompatible_model = ResolvedModel(
        reranker=Reranker(
            model=RecordingModel(),
            feature_columns=MODEL_FEATURE_COLUMNS[:-1],
            categorical_categories={},
        ),
        run_id="run-incompatible",
        model_version=None,
    )
    unavailable_dependencies = (
        (None, FakeFeatureBuilder()),
        (_resolved_model(), None),
        (incompatible_model, FakeFeatureBuilder()),
    )

    for resolved_model, feature_builder in unavailable_dependencies:
        app = create_app(resolved_model=resolved_model, feature_builder=feature_builder)
        with TestClient(app) as client:
            response = client.get("/healthcheck")
        assert response.status_code == 503


def test_metrics_observe_video_id_count() -> None:
    builder = FakeFeatureBuilder()
    app = create_app(resolved_model=_resolved_model(), feature_builder=builder)
    before = REGISTRY.get_sample_value("rerank_video_ids_count") or 0.0

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-1", "video-2"]},
        )

    after = REGISTRY.get_sample_value("rerank_video_ids_count") or 0.0
    assert response.status_code == 200
    assert after - before == 1.0


def test_rerank_maps_prediction_failure_to_500() -> None:
    resolved_model = ResolvedModel(
        reranker=Reranker(
            model=RaisingModel(),
            feature_columns=MODEL_FEATURE_COLUMNS,
            categorical_categories={},
        ),
        run_id="run-123",
        model_version="7",
    )
    app = create_app(resolved_model=resolved_model, feature_builder=FakeFeatureBuilder())

    with TestClient(app) as client:
        response = client.post(
            "/rerank", json={"user_id": "user-1", "video_ids": ["video-1"]}
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Reranking model returned an invalid prediction."}


def test_rerank_rejects_empty_video_ids_at_http_boundary() -> None:
    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())

    with TestClient(app) as client:
        response = client.post("/rerank", json={"user_id": "user-1", "video_ids": []})

    assert response.status_code == 422


def test_rerank_rejects_empty_string_id_at_http_boundary() -> None:
    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())

    with TestClient(app) as client:
        response = client.post(
            "/rerank", json={"user_id": "user-1", "video_ids": [""]}
        )

    assert response.status_code == 422
