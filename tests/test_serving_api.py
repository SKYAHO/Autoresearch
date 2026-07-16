from __future__ import annotations

import pickle
from pathlib import Path

import joblib
import numpy as np
import pytest
from fastapi.testclient import TestClient

from src.serving.app import create_app
from src.serving.model_loader import (
    LocalModelSettings,
    MlflowModelSettings,
    ModelArtifactError,
    load_local_model,
    load_mlflow_model,
)
from src.serving.schemas import CandidateVideo
from src.serving.service import Reranker


class RankingModel:
    def predict_proba(self, features):
        scores = features["ranking_signal"].to_numpy(dtype=float)
        return np.column_stack((1.0 - scores, scores))


class RaisingModel:
    def predict_proba(self, features):
        raise ValueError("boom")


def test_rerank_returns_500_when_model_raises() -> None:
    reranker = Reranker(
        model=RaisingModel(),
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )
    app = create_app(reranker=reranker)

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={
                "user_id": "user-1",
                "candidates": [
                    {"video_id": "video-1", "features": {"ranking_signal": 0.5}},
                ],
            },
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Reranking model returned an invalid prediction."}


class CategoricalCodeModel:
    """category 코드로 점수를 계산해 카테고리 매핑 정확성을 검증하는 모델."""

    def __init__(self) -> None:
        self.received: object = None

    def predict_proba(self, features):
        self.received = features
        codes = features["category_id"].cat.codes.to_numpy(dtype=float)
        scores = codes / 10.0
        return np.column_stack((1.0 - scores, scores))


def test_rerank_casts_categorical_columns_with_training_categories() -> None:
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

    # 학습 카테고리 순서(10, 20, 30) 기준 코드: 10 -> 0, 30 -> 2.
    # 요청에 없는 카테고리 20이 있어도 코드가 밀리지 않아야 한다.
    assert str(model.received["category_id"].dtype) == "category"
    assert list(model.received["category_id"].cat.categories) == [10, 20, 30]
    assert [item.video_id for item in response] == ["video-cat30", "video-cat10"]
    assert response[0].ctr_score == 0.2
    assert response[1].ctr_score == 0.0


def test_rerank_orders_candidates_by_ctr_score() -> None:
    reranker = Reranker(
        model=RankingModel(),
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )
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
    reranker = Reranker(
        model=RankingModel(),
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )
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
    categorical_columns_path = tmp_path / "categorical_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)
    with categorical_columns_path.open("wb") as categorical_columns_file:
        pickle.dump({}, categorical_columns_file)

    reranker = load_local_model(
        LocalModelSettings(
            model_path=model_path,
            feature_columns_path=feature_columns_path,
            categorical_columns_path=categorical_columns_path,
        )
    )

    response = reranker.rerank(
        [
            CandidateVideo(video_id="video-low", features={"ranking_signal": 0.1}),
            CandidateVideo(video_id="video-high", features={"ranking_signal": 0.8}),
        ]
    )

    assert [item.video_id for item in response] == ["video-high", "video-low"]


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
            LocalModelSettings(
                model_path=model_path,
                feature_columns_path=feature_columns_path,
                categorical_columns_path=categorical_columns_path,
            )
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
                model_path=model_path,
                feature_columns_path=feature_columns_path,
                categorical_columns_path=tmp_path / "missing.pkl",
            )
        )


def test_mlflow_model_loader_downloads_training_artifacts(
    tmp_path: Path, monkeypatch
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
        MlflowModelSettings(
            tracking_uri="http://mlflow.example",
            run_id="run-123",
        )
    )

    assert reranker.feature_columns == ("ranking_signal",)
    assert downloaded_uris == [
        "runs:/run-123/model/lgbm_model.joblib",
        "runs:/run-123/features/feature_columns.pkl",
        "runs:/run-123/features/categorical_columns.pkl",
    ]
