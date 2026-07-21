from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
import pickle
from pathlib import Path

import joblib
import numpy as np
import pandas as pd
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

import src.serving.app as serving_app
from src.serving.app import create_app
from src.serving.model_loader import (
    LocalModelSettings,
    MlflowModelSettings,
    ModelArtifactError,
    ResolvedModel,
    load_local_model,
    load_mlflow_model,
)
from src.serving.online_features import (
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
    FeatureRows,
    FeatureRetrievalError,
)
from src.serving.schemas import CandidateVideo, FeatureValue
from src.serving.service import PredictionError, Reranker


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
    settings = LocalModelSettings(Path("model"), Path("features"), Path("categories"))
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
    settings = LocalModelSettings(Path("model"), Path("features"), Path("categories"))
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

    with caplog.at_level("ERROR", logger="src.serving.app"):
        with TestClient(create_app()) as client:
            response = client.get("/healthcheck")

    assert response.status_code == 503
    assert feast_load_calls == ["feature_repo"]
    assert "phase=feature_store" in caplog.text
    assert "Feast initialization failed" not in caplog.text


def test_healthcheck_is_503_when_model_initialization_fails(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    settings = LocalModelSettings(Path("model"), Path("features"), Path("categories"))
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

    with caplog.at_level("ERROR", logger="src.serving.app"):
        with TestClient(create_app()) as client:
            response = client.get("/healthcheck")

    assert response.status_code == 503
    assert feast_load_calls == []
    assert "phase=model" in caplog.text
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


def test_metrics_observe_video_id_count() -> None:
    builder = FakeFeatureBuilder()
    app = create_app(resolved_model=_resolved_model(), feature_builder=builder)
    before = REGISTRY.get_sample_value("rerank_video_ids_sum") or 0.0

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={"user_id": "user-1", "video_ids": ["video-1", "video-2"]},
        )

    after = REGISTRY.get_sample_value("rerank_video_ids_sum") or 0.0
    assert response.status_code == 200
    assert after - before == 2.0


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


class RankingModel:
    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        scores = features["ranking_signal"].to_numpy(dtype=float)
        return np.column_stack((1.0 - scores, scores))


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


def test_metrics_report_unseen_category_type_mismatch_through_http() -> None:
    resolved_model = ResolvedModel(
        reranker=Reranker(
            model=CategoricalCodeModel(),
            feature_columns=MODEL_FEATURE_COLUMNS,
            categorical_categories={"category_id": (10, 20, 30)},
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
    assert scores["video-str"] == pytest.approx(-0.1)
    assert scores["video-int"] == pytest.approx(0.1)


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
    model_path = tmp_path / "model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    categorical_columns_path = tmp_path / "categorical_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)
    with categorical_columns_path.open("wb") as categorical_columns_file:
        pickle.dump({}, categorical_columns_file)

    reranker = load_local_model(
        LocalModelSettings(model_path, feature_columns_path, categorical_columns_path)
    )

    response = reranker.rerank(
        [
            CandidateVideo(video_id="video-low", features={"ranking_signal": 0.1}),
            CandidateVideo(video_id="video-high", features={"ranking_signal": 0.8}),
        ]
    )

    assert [item.video_id for item in response] == ["video-high", "video-low"]


def test_local_model_loader_preserves_categorical_value_types(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    categorical_columns_path = tmp_path / "categorical_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal", "category_id"], feature_columns_file)
    with categorical_columns_path.open("wb") as categorical_columns_file:
        pickle.dump({"category_id": [10, 20, 30]}, categorical_columns_file)

    reranker = load_local_model(
        LocalModelSettings(model_path, feature_columns_path, categorical_columns_path)
    )

    assert reranker.categorical_categories == {"category_id": (10, 20, 30)}
    assert all(type(category) is int for category in reranker.categorical_categories["category_id"])


def test_local_model_loader_rejects_unknown_categorical_columns(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    categorical_columns_path = tmp_path / "categorical_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)
    with categorical_columns_path.open("wb") as categorical_columns_file:
        pickle.dump({"unknown_column": [1, 2]}, categorical_columns_file)

    with pytest.raises(ModelArtifactError):
        load_local_model(
            LocalModelSettings(model_path, feature_columns_path, categorical_columns_path)
        )


def test_local_model_loader_requires_categorical_artifact(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)

    with pytest.raises(ModelArtifactError):
        load_local_model(
            LocalModelSettings(
                model_path,
                feature_columns_path,
                tmp_path / "missing.pkl",
            )
        )


def test_mlflow_model_loader_downloads_training_artifacts(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    model_path = tmp_path / "lgbm_model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    categorical_columns_path = tmp_path / "categorical_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)
    with categorical_columns_path.open("wb") as categorical_columns_file:
        pickle.dump({}, categorical_columns_file)
    downloaded_uris: list[str] = []

    def download_artifacts(*, artifact_uri: str) -> str:
        downloaded_uris.append(artifact_uri)
        if artifact_uri.endswith("lgbm_model.joblib"):
            return str(model_path)
        if artifact_uri.endswith("categorical_columns.pkl"):
            return str(categorical_columns_path)
        return str(feature_columns_path)

    monkeypatch.setattr(
        "src.serving.model_loader.mlflow.artifacts.download_artifacts",
        download_artifacts,
    )

    reranker = load_mlflow_model(
        MlflowModelSettings(tracking_uri="http://mlflow.example", run_id="run-123")
    )

    assert reranker.feature_columns == ("ranking_signal",)
    assert downloaded_uris == [
        "runs:/run-123/model/lgbm_model.joblib",
        "runs:/run-123/features/feature_columns.pkl",
        "runs:/run-123/features/categorical_columns.pkl",
    ]
