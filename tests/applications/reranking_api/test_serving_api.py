from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import json
from pathlib import Path
from typing import BinaryIO

import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY
from prometheus_client.parser import text_string_to_metric_families

import applications.reranking_api.app as serving_app
import applications.reranking_api.model_loader as model_loader_module
from autoresearch.feature_engineering.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    FeatureContractError,
    MODEL_FEATURE_COLUMNS,
)
from applications.reranking_api.app import create_app
from applications.reranking_api.model_loader import (
    LocalModelSettings,
    MlflowModelSettings,
    ModelArtifactError,
    ResolvedModel,
    load_local_model,
    load_mlflow_model,
)
from applications.reranking_api.online_features import (
    FeatureBuildTimings,
    FeatureRows,
    FeatureRetrievalError,
    TimedFeatureBuild,
)
from applications.reranking_api.schemas import CandidateVideo, FeatureValue, RerankedVideo
from applications.reranking_api.service import PredictionError, RerankOutcome, Reranker
from autoresearch.model_registry.model_package import ModelPackageManifest, save_manifest


class _OnnxMetadata:
    def __init__(self, name: str, type_: str, shape: list[object]) -> None:
        self.name = name
        self.type = type_
        self.shape = shape


class _FakeOnnxSession:
    def get_inputs(self):
        return [_OnnxMetadata("input", "tensor(float)", [None, len(MODEL_FEATURE_COLUMNS)])]

    def get_outputs(self):
        return [_OnnxMetadata("probabilities", "tensor(float)", [None, 2])]

    def run(self, _outputs, feeds):
        matrix = next(iter(feeds.values()))
        scores = matrix[:, MODEL_FEATURE_COLUMNS.index("view_count")]
        return [np.column_stack((1.0 - scores, scores)).astype(np.float32)]


@pytest.fixture(autouse=True)
def _stub_local_onnx_session(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        model_loader_module,
        "_build_onnx_session_from_path",
        lambda _path: _FakeOnnxSession(),
    )


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

    def build_with_timings(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> TimedFeatureBuild:
        return TimedFeatureBuild(
            candidates=self.build(
                user_id=user_id,
                video_ids=video_ids,
                feature_columns=feature_columns,
            ),
            timings=FeatureBuildTimings(0.01, 0.02, 0.03),
        )


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

    def build_with_timings(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> TimedFeatureBuild:
        return TimedFeatureBuild(
            candidates=self.build(
                user_id=user_id,
                video_ids=video_ids,
                feature_columns=feature_columns,
            ),
            timings=FeatureBuildTimings(0.01, 0.02, 0.03),
        )


@dataclass
class ContractFailingFeatureBuilder:
    def build(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> list[CandidateVideo]:
        raise FeatureContractError(reason="model and feature-store contract disagree")

    def build_with_timings(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> TimedFeatureBuild:
        return TimedFeatureBuild(
            candidates=self.build(
                user_id=user_id,
                video_ids=video_ids,
                feature_columns=feature_columns,
            ),
            timings=FeatureBuildTimings(0.01, 0.02, 0.03),
        )


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


class FakeOnlineFeatureReader:
    def read(
        self,
        *,
        feature_refs: Sequence[str],
        entity_rows: Sequence[Mapping[str, str]],
    ) -> FeatureRows:
        raise AssertionError("healthcheck must not read online features")


def test_lifespan_loads_model_and_feast_builder_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    settings = LocalModelSettings(Path("model.onnx"), Path("features"), Path("categories"), Path("manifest"))
    model_settings_calls: list[None] = []
    model_load_calls: list[LocalModelSettings] = []
    feast_load_calls: list[str] = []

    monkeypatch.setenv("RERANK_FEATURE_REPO_PATH", "custom-feature-repo")
    monkeypatch.setattr(
        serving_app,
        "load_model_settings_from_environment",
        lambda: model_settings_calls.append(None) or settings,
        raising=False,
    )
    monkeypatch.setattr(
        serving_app,
        "load_reranker_with_lineage",
        lambda received: model_load_calls.append(received) or _resolved_model(),
        raising=False,
    )
    monkeypatch.setattr(
        serving_app,
        "load_feast_online_feature_reader",
        lambda repo_path: feast_load_calls.append(repo_path) or FakeOnlineFeatureReader(),
        raising=False,
    )

    with TestClient(create_app()) as client:
        response = client.get("/healthcheck")

    assert response.status_code == 200
    assert model_settings_calls == [None]
    assert model_load_calls == [settings]
    assert feast_load_calls == ["custom-feature-repo"]


def test_healthcheck_is_503_when_feast_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = LocalModelSettings(Path("model.onnx"), Path("features"), Path("categories"), Path("manifest"))
    feast_load_calls: list[str] = []

    monkeypatch.setattr(
        serving_app,
        "load_model_settings_from_environment",
        lambda: settings,
        raising=False,
    )
    monkeypatch.setattr(
        serving_app,
        "load_reranker_with_lineage",
        lambda received: _resolved_model(),
        raising=False,
    )

    def fail_feast_load(repo_path: str) -> FakeOnlineFeatureReader:
        feast_load_calls.append(repo_path)
        raise RuntimeError("Feast initialization failed")

    monkeypatch.setattr(
        serving_app,
        "load_feast_online_feature_reader",
        fail_feast_load,
        raising=False,
    )

    with caplog.at_level("ERROR", logger="applications.reranking_api.app"):
        with TestClient(create_app()) as client:
            response = client.get("/healthcheck")

    assert response.status_code == 503
    assert feast_load_calls == ["feature_repo"]
    assert "phase=feature_store" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "Feast initialization failed" not in caplog.text


def test_healthcheck_is_503_when_model_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = LocalModelSettings(Path("model.onnx"), Path("features"), Path("categories"), Path("manifest"))
    feast_load_calls: list[str] = []

    monkeypatch.setattr(
        serving_app,
        "load_model_settings_from_environment",
        lambda: settings,
    )

    def fail_model_load(received: LocalModelSettings) -> ResolvedModel:
        raise RuntimeError("token=secret-model-loader-error")

    monkeypatch.setattr(serving_app, "load_reranker_with_lineage", fail_model_load)
    monkeypatch.setattr(
        serving_app,
        "load_feast_online_feature_reader",
        lambda repo_path: feast_load_calls.append(repo_path) or FakeOnlineFeatureReader(),
    )

    with caplog.at_level("ERROR", logger="applications.reranking_api.app"):
        with TestClient(create_app()) as client:
            response = client.get("/healthcheck")

    assert response.status_code == 503
    assert feast_load_calls == []
    assert "phase=model" in caplog.text
    assert "error_type=RuntimeError" in caplog.text
    assert "secret-model-loader-error" not in caplog.text


def test_lifespan_skips_environment_factories_for_injected_dependencies(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def unexpected_factory(*args: str | LocalModelSettings) -> None:
        raise AssertionError("injected app must not call environment factories")

    monkeypatch.setattr(
        serving_app,
        "load_model_settings_from_environment",
        unexpected_factory,
        raising=False,
    )
    monkeypatch.setattr(
        serving_app,
        "load_reranker_with_lineage",
        unexpected_factory,
        raising=False,
    )
    monkeypatch.setattr(
        serving_app,
        "load_feast_online_feature_reader",
        unexpected_factory,
        raising=False,
    )

    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())
    with TestClient(app) as client:
        response = client.get("/healthcheck")

    assert response.status_code == 200


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


def test_rerank_preserves_requested_video_id_order_with_model_run_id() -> None:
    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-low", "video-high"]},
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"video_id": "video-low", "ctr_score": 0.1, "model_id": "run-123"},
            {"video_id": "video-high", "ctr_score": 0.2, "model_id": "run-123"},
        ]
    }


def test_openapi_describes_rerank_response_in_request_order() -> None:
    # Given: the public serving application.
    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())

    # When: a client reads the generated OpenAPI response schema.
    openapi_schema = app.openapi()
    response_description = openapi_schema["components"]["schemas"]["RerankResponse"][
        "description"
    ]

    # Then: the schema promises request order rather than score order.
    assert response_description == (
        "/rerank 응답 본문. items는 요청 video_ids 순서를 보존한다."
    )


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


def test_rerank_maps_feature_contract_failure_to_503() -> None:
    app = create_app(
        resolved_model=_resolved_model(), feature_builder=ContractFailingFeatureBuilder()
    )

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-1"]},
        )

    assert response.status_code == 503
    assert response.json() == {"detail": "model and feature-store contract disagree"}


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


def _sample_value(
    client: TestClient, name: str, labels: Mapping[str, str]
) -> float:
    for family in text_string_to_metric_families(client.get("/metrics").text):
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return float(sample.value)
    return 0.0


def test_rerank_observes_fixed_phases_success_and_in_flight() -> None:
    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())

    with TestClient(app) as client:
        before = _sample_value(client, "rerank_outcomes_total", {"outcome": "success"})
        response = client.post(
            "/rerank", json={"user_id": "user-1", "video_ids": ["video-1"]}
        )
        after = _sample_value(client, "rerank_outcomes_total", {"outcome": "success"})
        in_flight = _sample_value(client, "rerank_in_flight", {})
        text = client.get("/metrics").text

    assert response.status_code == 200
    assert after == before + 1
    assert in_flight == 0
    assert "feature_read_first" in text and "response_build" in text
    assert "user-1" not in text and "video-1" not in text and "run-123" not in text


def test_rerank_observes_feature_and_prediction_outcome_metrics() -> None:
    feature_app = create_app(
        resolved_model=_resolved_model(), feature_builder=FailingFeatureBuilder()
    )
    prediction_model = ResolvedModel(
        reranker=Reranker(
            model=RaisingModel(),
            feature_columns=MODEL_FEATURE_COLUMNS,
            categorical_categories={},
        ),
        run_id="run-123",
        model_version="7",
    )
    prediction_app = create_app(
        resolved_model=prediction_model, feature_builder=FakeFeatureBuilder()
    )

    with TestClient(feature_app) as client:
        before = _sample_value(
            client, "rerank_outcomes_total", {"outcome": "feature_error"}
        )
        feature_response = client.post(
            "/rerank", json={"user_id": "user-1", "video_ids": ["video-1"]}
        )
        after = _sample_value(
            client, "rerank_outcomes_total", {"outcome": "feature_error"}
        )
        feature_in_flight = _sample_value(client, "rerank_in_flight", {})

    with TestClient(prediction_app) as client:
        before_prediction = _sample_value(
            client, "rerank_outcomes_total", {"outcome": "prediction_error"}
        )
        prediction_response = client.post(
            "/rerank", json={"user_id": "user-1", "video_ids": ["video-1"]}
        )
        after_prediction = _sample_value(
            client, "rerank_outcomes_total", {"outcome": "prediction_error"}
        )
        prediction_in_flight = _sample_value(client, "rerank_in_flight", {})

    assert feature_response.status_code == 503
    assert after == before + 1
    assert feature_in_flight == 0
    assert prediction_response.status_code == 500
    assert after_prediction == before_prediction + 1
    assert prediction_in_flight == 0


def test_rerank_observes_unavailable_outcome_metric() -> None:
    app = create_app(resolved_model=None, feature_builder=FakeFeatureBuilder())

    with TestClient(app) as client:
        before = _sample_value(client, "rerank_outcomes_total", {"outcome": "unavailable"})
        response = client.post(
            "/rerank", json={"user_id": "user-1", "video_ids": ["video-1"]}
        )
        after = _sample_value(client, "rerank_outcomes_total", {"outcome": "unavailable"})
        in_flight = _sample_value(client, "rerank_in_flight", {})

    assert response.status_code == 503
    assert after == before + 1
    assert in_flight == 0


def test_metrics_scrape_observes_legacy_and_current_video_id_count() -> None:
    # Given: both metric identities start from their current registry values.
    builder = FakeFeatureBuilder()
    app = create_app(resolved_model=_resolved_model(), feature_builder=builder)
    metric_names = (
        "rerank_video_ids_count",
        "rerank_video_ids_sum",
        "rerank_candidates_count",
        "rerank_candidates_sum",
    )
    before = {
        metric_name: REGISTRY.get_sample_value(metric_name) or 0.0
        for metric_name in metric_names
    }

    # When: one request with two IDs is followed by a Prometheus scrape.
    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-1", "video-2"]},
        )
        metrics_response = client.get("/metrics")

    scraped = {
        sample.name: sample.value
        for family in text_string_to_metric_families(metrics_response.text)
        for sample in family.samples
        if sample.name in metric_names
    }

    # Then: current and deprecated identities report the same request volume.
    assert response.status_code == 200
    assert metrics_response.status_code == 200
    assert "# HELP rerank_candidates DEPRECATED:" in metrics_response.text
    assert "migrate to rerank_video_ids." in metrics_response.text
    assert {
        metric_name: scraped[metric_name] - before[metric_name]
        for metric_name in metric_names
    } == {
        "rerank_video_ids_count": 1.0,
        "rerank_video_ids_sum": 2.0,
        "rerank_candidates_count": 1.0,
        "rerank_candidates_sum": 2.0,
    }


def test_metrics_describe_full_serving_readiness_without_renaming_metric() -> None:
    # Given: a runtime with a compatible model and online feature builder.
    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())

    # When: Prometheus scrapes the metrics endpoint.
    with TestClient(app) as client:
        response = client.get("/metrics")

    # Then: the existing metric describes every readiness dependency.
    assert response.status_code == 200
    assert (
        "# HELP rerank_model_ready Whether the model, online feature store, and "
        "feature contract are ready."
    ) in response.text


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


@pytest.mark.parametrize(
    "outcome_video_ids",
    (
        ("video-1",),
        ("video-1", "video-1"),
        ("video-1", "video-2", "video-extra"),
    ),
)
def test_rerank_maps_invalid_outcome_video_ids_to_safe_500(
    monkeypatch: pytest.MonkeyPatch,
    outcome_video_ids: tuple[str, ...],
) -> None:
    # Given: the reranker returns missing, duplicate, or extra video IDs.
    def invalid_outcome(
        _reranker: Reranker,
        _candidates: Sequence[CandidateVideo],
    ) -> RerankOutcome:
        return RerankOutcome(
            items=[
                RerankedVideo(video_id=video_id, ctr_score=0.5)
                for video_id in outcome_video_ids
            ],
            unseen_categories={},
        )

    monkeypatch.setattr(Reranker, "rerank_with_diagnostics", invalid_outcome)
    app = create_app(
        resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder()
    )

    # When: the invalid outcome reaches the HTTP response mapping boundary.
    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-1", "video-2"]},
        )

    # Then: callers receive the existing safe prediction failure response.
    assert response.status_code == 500
    assert response.json() == {"detail": "Reranking model returned an invalid prediction."}


@pytest.mark.parametrize(
    "payload",
    (
        {"user_id": "", "video_ids": ["video-1"]},
        {"user_id": "user-1", "video_ids": []},
        {"user_id": "user-1", "video_ids": [""]},
        {
            "user_id": "user-1",
            "video_ids": [f"video-{index}" for index in range(201)],
        },
        {"user_id": "user-1", "video_ids": ["video-1", "video-1"]},
    ),
)
def test_rerank_rejects_invalid_request_ids_before_builder(
    payload: dict[str, str | list[str]],
) -> None:
    # Given: a builder that records every invocation.
    builder = FakeFeatureBuilder()
    app = create_app(resolved_model=_resolved_model(), feature_builder=builder)

    # When: an invalid request reaches the HTTP validation boundary.
    with TestClient(app) as client:
        response = client.post("/rerank", json=payload)

    # Then: validation rejects it before online feature construction.
    assert response.status_code == 422
    assert builder.calls == []


class RankingModel:
    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        scores = features["view_count"].to_numpy(dtype=float)
        return np.column_stack((1.0 - scores, scores))


def _write_local_model_artifacts(
    tmp_path: Path,
    *,
    feature_columns: Sequence[str] = MODEL_FEATURE_COLUMNS,
    categorical_columns: Sequence[str] = CATEGORICAL_FEATURE_COLUMNS,
    category_values: Mapping[str, Sequence[str | int | float | bool]] | None = None,
) -> LocalModelSettings:
    onnx_dir = tmp_path / "model_onnx"
    onnx_dir.mkdir(parents=True, exist_ok=True)
    onnx_path = onnx_dir / "model.onnx"
    onnx_path.write_bytes(b"test-onnx")
    feature_columns_path = tmp_path / "features" / "feature_columns.json"
    categorical_columns_path = tmp_path / "features" / "categorical_columns.json"
    feature_columns_path.parent.mkdir(parents=True, exist_ok=True)
    categories = {
        column: list(category_values.get(column, ())) if category_values is not None else []
        for column in categorical_columns
    }
    with feature_columns_path.open("w", encoding="utf-8") as feature_columns_file:
        json.dump(list(feature_columns), feature_columns_file)
    with categorical_columns_path.open("w", encoding="utf-8") as categorical_columns_file:
        json.dump(categories, categorical_columns_file)
    manifest = ModelPackageManifest.build(
        sampling_rate=1.0,
        model_onnx=onnx_dir,
        feature_columns=feature_columns_path,
        categorical_columns=categorical_columns_path,
        calibration=None,
    )
    manifest_path = tmp_path / "manifest" / "manifest.json"
    save_manifest(manifest, manifest_path)
    return LocalModelSettings(
        onnx_path, feature_columns_path, categorical_columns_path, manifest_path
    )


class CategoricalCodeModel:
    def __init__(self) -> None:
        self.received: pd.DataFrame | None = None

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        self.received = features
        scores = features["category_id"].cat.codes.to_numpy(dtype=float) / 10.0
        return np.column_stack((1.0 - scores, scores))


@dataclass
class CategoricalFeatureBuilder:
    def build(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> list[CandidateVideo]:
        category_ids: tuple[FeatureValue, ...] = ("10", 20)
        return [
            CandidateVideo(
                video_id=video_id,
                features={
                    **{column: 1.0 for column in MODEL_FEATURE_COLUMNS},
                    "category_id": category_ids[index],
                },
            )
            for index, video_id in enumerate(video_ids)
        ]

    def build_with_timings(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> TimedFeatureBuild:
        return TimedFeatureBuild(
            candidates=self.build(
                user_id=user_id,
                video_ids=video_ids,
                feature_columns=feature_columns,
            ),
            timings=FeatureBuildTimings(0.01, 0.02, 0.03),
        )


def test_healthcheck_rejects_non_string_categories_for_string_serving_features() -> None:
    builder = FakeFeatureBuilder()
    resolved_model = ResolvedModel(
        reranker=Reranker(
            model=CategoricalCodeModel(),
            feature_columns=MODEL_FEATURE_COLUMNS,
            categorical_categories={"category_id": (10, 20, 30)},
        ),
        run_id="run-incompatible-category-type",
        model_version=None,
    )
    app = create_app(resolved_model=resolved_model, feature_builder=builder)

    with TestClient(app) as client:
        healthcheck = client.get("/healthcheck")
        rerank = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-1"]},
        )

    assert healthcheck.status_code == 503
    assert rerank.status_code == 503
    assert builder.calls == []


def test_healthcheck_rejects_non_string_watch_time_band_categories() -> None:
    resolved_model = ResolvedModel(
        reranker=Reranker(
            model=RecordingModel(),
            feature_columns=MODEL_FEATURE_COLUMNS,
            categorical_categories={"watch_time_band": (1, 2)},
        ),
        run_id="run-incompatible-watch-time-band-type",
        model_version=None,
    )
    builder = FakeFeatureBuilder()
    app = create_app(resolved_model=resolved_model, feature_builder=builder)

    with TestClient(app) as client:
        healthcheck = client.get("/healthcheck")
        rerank = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-1"]},
        )

    assert healthcheck.status_code == 503
    assert rerank.status_code == 503
    assert builder.calls == []


def test_metrics_report_unseen_category_type_mismatch_through_http() -> None:
    resolved_model = ResolvedModel(
        reranker=Reranker(
            model=CategoricalCodeModel(),
            feature_columns=MODEL_FEATURE_COLUMNS,
            categorical_categories={"category_id": ("10", "20", "30")},
        ),
        run_id="run-categorical",
        model_version=None,
    )
    app = create_app(
        resolved_model=resolved_model,
        feature_builder=CategoricalFeatureBuilder(),
    )
    labels = {"column": "category_id"}
    before = REGISTRY.get_sample_value("rerank_unseen_category_total", labels) or 0.0

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-str", "video-int"]},
        )

    after = REGISTRY.get_sample_value("rerank_unseen_category_total", labels) or 0.0

    assert response.status_code == 200
    assert after - before == 1.0
    scores = {item["video_id"]: item["ctr_score"] for item in response.json()["items"]}
    assert scores["video-str"] == pytest.approx(0.0)
    assert scores["video-int"] == pytest.approx(-0.1)


def test_rerank_preserves_training_categorical_codes() -> None:
    model = CategoricalCodeModel()
    reranker = Reranker(
        model=model,
        feature_columns=("category_id",),
        categorical_categories={"category_id": (10, 20, 30)},
    )

    response = reranker.rerank(
        [
            CandidateVideo(video_id="video-cat10", features={"category_id": 10}),
            CandidateVideo(video_id="video-cat30", features={"category_id": 30}),
        ]
    )

    assert model.received is not None
    assert str(model.received["category_id"].dtype) == "category"
    assert list(model.received["category_id"].cat.categories) == [10, 20, 30]
    assert [item.video_id for item in response] == ["video-cat30", "video-cat10"]


def test_rerank_reports_unseen_categorical_values_without_coercion() -> None:
    reranker = Reranker(
        model=CategoricalCodeModel(),
        feature_columns=("category_id",),
        categorical_categories={"category_id": (10, 20, 30)},
    )

    outcome = reranker.rerank_with_diagnostics(
        [
            CandidateVideo(video_id="video-known", features={"category_id": 10}),
            CandidateVideo(video_id="video-unseen", features={"category_id": 99}),
            CandidateVideo(video_id="video-string", features={"category_id": "10"}),
        ]
    )

    assert outcome.unseen_categories == {"category_id": (99, "10")}


class WrongShapeModel:
    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        return np.zeros((len(features), 3))


class ListReturningModel:
    def predict_proba(self, features: pd.DataFrame) -> list[float]:
        return [0.0] * len(features)


@pytest.mark.parametrize("model", (WrongShapeModel(), ListReturningModel()))
def test_rerank_rejects_invalid_prediction_shapes(
    model: WrongShapeModel | ListReturningModel,
) -> None:
    reranker = Reranker(
        model=model,
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )

    with pytest.raises(PredictionError):
        reranker.rerank(
            [CandidateVideo(video_id="video-1", features={"ranking_signal": 0.5})]
        )


def test_local_model_loader_reads_model_and_feature_columns(tmp_path: Path) -> None:
    reranker = load_local_model(_write_local_model_artifacts(tmp_path))

    response = reranker.rerank(
        [
            CandidateVideo(
                video_id="video-low",
                features={column: 0.1 for column in MODEL_FEATURE_COLUMNS},
            ),
            CandidateVideo(
                video_id="video-high",
                features={column: 0.8 for column in MODEL_FEATURE_COLUMNS},
            ),
        ]
    )

    assert reranker.feature_columns == MODEL_FEATURE_COLUMNS
    assert tuple(reranker.categorical_categories) == CATEGORICAL_FEATURE_COLUMNS
    assert [item.video_id for item in response] == ["video-high", "video-low"]


def test_local_model_loader_rejects_truncated_feature_columns_artifact(
    tmp_path: Path,
) -> None:
    settings = _write_local_model_artifacts(tmp_path)
    settings.feature_columns_path.write_bytes(b"[")  # 잘린 JSON

    with pytest.raises(ModelArtifactError, match="패키지 검증 실패") as error_info:
        load_local_model(settings)

    assert "feature_columns SHA-256" in str(error_info.value)


def test_local_model_loader_rejects_malformed_categorical_columns_artifact(
    tmp_path: Path,
) -> None:
    settings = _write_local_model_artifacts(tmp_path)
    settings.categorical_columns_path.write_bytes(b"not json")

    with pytest.raises(ModelArtifactError, match="패키지 검증 실패") as error_info:
        load_local_model(settings)

    assert "categorical_columns SHA-256" in str(error_info.value)


def test_local_model_loader_normalizes_unreadable_metadata_artifact(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    settings = _write_local_model_artifacts(tmp_path)
    read_error = PermissionError("metadata file is unreadable")
    original_open = Path.open

    def fail_for_feature_columns(
        path: Path,
        mode: str = "r",
        buffering: int = -1,
        encoding: str | None = None,
        errors: str | None = None,
        newline: str | None = None,
    ) -> BinaryIO:
        if path == settings.feature_columns_path:
            raise read_error
        return original_open(
            path,
            mode=mode,
            buffering=buffering,
            encoding=encoding,
            errors=errors,
            newline=newline,
        )

    monkeypatch.setattr(Path, "open", fail_for_feature_columns)

    with pytest.raises(ModelArtifactError, match="패키지 검증 실패") as error_info:
        load_local_model(settings)

    assert error_info.value.__cause__ is read_error


def test_local_model_loader_normalizes_schema_malformed_metadata_artifact(
    tmp_path: Path,
) -> None:
    settings = _write_local_model_artifacts(tmp_path)
    settings.feature_columns_path.write_text(
        json.dumps({"not": "columns"}), encoding="utf-8"
    )

    with pytest.raises(ModelArtifactError, match="패키지 검증 실패") as error_info:
        load_local_model(settings)

    assert "feature_columns SHA-256" in str(error_info.value)


def test_local_model_loader_preserves_categorical_value_types(tmp_path: Path) -> None:
    reranker = load_local_model(
        _write_local_model_artifacts(
            tmp_path,
            category_values={"category_id": (10, 20, 30)},
        )
    )

    assert reranker.categorical_categories["category_id"] == (10, 20, 30)
    assert all(
        type(category) is int
        for category in reranker.categorical_categories["category_id"]
    )


@pytest.mark.parametrize(
    ("feature_columns", "categorical_columns", "artifact_context"),
    (
        (MODEL_FEATURE_COLUMNS[:-1], CATEGORICAL_FEATURE_COLUMNS, "Feature-column artifact"),
        (
            MODEL_FEATURE_COLUMNS + ("extra_feature",),
            CATEGORICAL_FEATURE_COLUMNS,
            "Feature-column artifact",
        ),
        (
            MODEL_FEATURE_COLUMNS[1:] + MODEL_FEATURE_COLUMNS[:1],
            CATEGORICAL_FEATURE_COLUMNS,
            "Feature-column artifact",
        ),
        (MODEL_FEATURE_COLUMNS, CATEGORICAL_FEATURE_COLUMNS[:-1], "Categorical-column artifact"),
        (
            MODEL_FEATURE_COLUMNS,
            CATEGORICAL_FEATURE_COLUMNS + ("unknown_column",),
            "Categorical-column artifact",
        ),
        (
            MODEL_FEATURE_COLUMNS,
            tuple(reversed(CATEGORICAL_FEATURE_COLUMNS)),
            "Categorical-column artifact",
        ),
    ),
)
def test_local_model_loader_rejects_noncanonical_artifact_metadata(
    tmp_path: Path,
    feature_columns: Sequence[str],
    categorical_columns: Sequence[str],
    artifact_context: str,
) -> None:
    settings = _write_local_model_artifacts(
        tmp_path,
        feature_columns=feature_columns,
        categorical_columns=categorical_columns,
    )

    with pytest.raises(ModelArtifactError, match=artifact_context) as error_info:
        load_local_model(settings)

    assert isinstance(error_info.value.__cause__, FeatureContractError)


def test_local_model_loader_requires_categorical_artifact(tmp_path: Path) -> None:
    settings = _write_local_model_artifacts(tmp_path)

    with pytest.raises(ModelArtifactError):
        load_local_model(
            LocalModelSettings(
                settings.onnx_model_path,
                settings.feature_columns_path,
                tmp_path / "missing.json",
                settings.manifest_path,
            )
        )


def test_mlflow_model_loader_fails_closed_without_manifest(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    downloaded_uris: list[str] = []

    def download_artifacts(*, artifact_uri: str, dst_path: str) -> str:
        downloaded_uris.append(artifact_uri)
        raise FileNotFoundError(f"manifest does not exist in {dst_path}")

    monkeypatch.setattr(
        "applications.reranking_api.model_loader.mlflow.artifacts.download_artifacts",
        download_artifacts,
    )

    with pytest.raises(ModelArtifactError, match="manifest"):
        load_mlflow_model(
            MlflowModelSettings(tracking_uri="http://mlflow.example", run_id="run-123")
        )
    assert downloaded_uris == ["runs:/run-123/manifest/manifest.json"]
