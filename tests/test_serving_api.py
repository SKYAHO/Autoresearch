from __future__ import annotations

import pickle
from pathlib import Path

import joblib
import numpy as np
from fastapi.testclient import TestClient

from src.serving.app import create_app
from src.serving.model_loader import (
    LocalModelSettings,
    MlflowModelSettings,
    load_local_model,
    load_mlflow_model,
)
from src.serving.schemas import CandidateVideo
from src.serving.service import Reranker


class RankingModel:
    def predict_proba(self, features):
        scores = features["ranking_signal"].to_numpy(dtype=float)
        return np.column_stack((1.0 - scores, scores))


def test_rerank_orders_candidates_by_ctr_score() -> None:
    reranker = Reranker(model=RankingModel(), feature_columns=("ranking_signal",))
    app = create_app(reranker=reranker)

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={
                "user_id": "user-1",
                "candidates": [
                    {"video_id": "video-low", "features": {"ranking_signal": 0.2}},
                    {"video_id": "video-high", "features": {"ranking_signal": 0.9}},
                    {"video_id": "video-mid", "features": {"ranking_signal": 0.5}},
                ],
            },
        )

    assert response.status_code == 200
    assert response.json() == {
        "items": [
            {"video_id": "video-high", "ctr_score": 0.9},
            {"video_id": "video-mid", "ctr_score": 0.5},
            {"video_id": "video-low", "ctr_score": 0.2},
        ]
    }


def test_healthcheck_and_metrics_report_ready_model() -> None:
    reranker = Reranker(model=RankingModel(), feature_columns=("ranking_signal",))
    app = create_app(reranker=reranker)

    with TestClient(app) as client:
        health_response = client.get("/healthcheck")
        metrics_response = client.get("/metrics")

    assert health_response.status_code == 200
    assert health_response.json() == {"status": "ok"}
    assert metrics_response.status_code == 200
    assert "rerank_requests_total" in metrics_response.text
    assert "rerank_model_ready" in metrics_response.text


def test_local_model_loader_reads_model_and_feature_columns(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)

    reranker = load_local_model(
        LocalModelSettings(
            model_path=model_path,
            feature_columns_path=feature_columns_path,
        )
    )

    response = reranker.rerank(
        [
            CandidateVideo(video_id="video-low", features={"ranking_signal": 0.1}),
            CandidateVideo(video_id="video-high", features={"ranking_signal": 0.8}),
        ]
    )

    assert [item.video_id for item in response] == ["video-high", "video-low"]


def test_mlflow_model_loader_downloads_training_artifacts(
    tmp_path: Path, monkeypatch
) -> None:
    model_path = tmp_path / "lgbm_model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)

    downloaded_uris: list[str] = []

    def download_artifacts(*, artifact_uri: str) -> str:
        downloaded_uris.append(artifact_uri)
        if artifact_uri.endswith("lgbm_model.joblib"):
            return str(model_path)
        return str(feature_columns_path)

    monkeypatch.setattr(
        "src.serving.model_loader.mlflow.artifacts.download_artifacts",
        download_artifacts,
    )

    reranker = load_mlflow_model(
        MlflowModelSettings(
            tracking_uri="http://mlflow.example",
            run_id="run-123",
        )
    )

    assert reranker.feature_columns == ("ranking_signal",)
    assert downloaded_uris == [
        "runs:/run-123/model/lgbm_model.joblib",
        "runs:/run-123/features/feature_columns.pkl",
    ]
