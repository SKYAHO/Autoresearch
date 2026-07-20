from __future__ import annotations

import pickle
from pathlib import Path

import joblib
import numpy as np
import pytest
from fastapi.testclient import TestClient
from prometheus_client import REGISTRY

from src.serving.app import create_app
from src.serving.model_loader import (
    LocalModelSettings,
    MlflowModelSettings,
    ModelArtifactError,
    load_local_model,
    load_mlflow_model,
)
from src.serving.schemas import CandidateVideo
from src.serving.service import PredictionError, Reranker


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


def test_rerank_with_diagnostics_reports_unseen_categories() -> None:
    reranker = Reranker(
        model=CategoricalCodeModel(),
        feature_columns=("category_id",),
        categorical_categories={"category_id": (10, 20, 30)},
    )

    outcome = reranker.rerank_with_diagnostics(
        [
            CandidateVideo(video_id="video-known", features={"category_id": 10}),
            CandidateVideo(video_id="video-unseen", features={"category_id": 99}),
        ]
    )

    # 학습에 없던 category_id=99 는 NaN으로 강등되고, 진단에 원래 값이 보고되어야 한다.
    assert outcome.unseen_categories == {"category_id": (99,)}
    assert {item.video_id for item in outcome.items} == {"video-known", "video-unseen"}


def test_rerank_detects_categorical_type_mismatch_as_unseen() -> None:
    # 요청 categorical 값의 타입이 학습 카테고리와 다르면(str "10" vs int 10) 조용히 NaN으로
    # 강등된다. 정규화(coerce)하지 않는 detection-only 동작을 고정한다 — 예방이 아니라 감지:
    # 요청은 실패하지 않고, unseen 진단으로 원래 값이 그대로 보고되어야 한다.
    reranker = Reranker(
        model=CategoricalCodeModel(),
        feature_columns=("category_id",),
        categorical_categories={"category_id": (10, 20, 30)},
    )

    outcome = reranker.rerank_with_diagnostics(
        [
            CandidateVideo(video_id="video-1", features={"category_id": "10"}),
            CandidateVideo(video_id="video-2", features={"category_id": "20"}),
        ]
    )

    assert outcome.unseen_categories == {"category_id": ("10", "20")}


def test_rerank_with_diagnostics_empty_when_all_categories_known() -> None:
    reranker = Reranker(
        model=CategoricalCodeModel(),
        feature_columns=("category_id",),
        categorical_categories={"category_id": (10, 20, 30)},
    )

    outcome = reranker.rerank_with_diagnostics(
        [
            CandidateVideo(video_id="video-a", features={"category_id": 10}),
            CandidateVideo(video_id="video-b", features={"category_id": 30}),
        ]
    )

    assert outcome.unseen_categories == {}


def test_metrics_report_unseen_category_coercions() -> None:
    reranker = Reranker(
        model=CategoricalCodeModel(),
        feature_columns=("category_id",),
        categorical_categories={"category_id": (10, 20, 30)},
    )
    app = create_app(reranker=reranker)

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={
                "user_id": "user-1",
                "candidates": [
                    {"video_id": "video-known", "features": {"category_id": 10}},
                    {"video_id": "video-unseen", "features": {"category_id": 99}},
                ],
            },
        )
        metrics_response = client.get("/metrics")

    # 학습에 없던 카테고리가 트래픽에 등장해도 요청은 200으로 응답된다(조용한 degradation).
    assert response.status_code == 200
    # 그 대신 unseen-category 카운터가 컬럼별로 계측되어 감지 가능해야 한다.
    assert 'rerank_unseen_category_total{column="category_id"}' in metrics_response.text


def test_metrics_report_type_mismatch_through_http_request() -> None:
    # 타입 불일치 감지가 단위 호출뿐 아니라 실제 HTTP 경로에서도 성립하는지 고정한다.
    # 핵심 연결 고리는 pydantic smart union이다 — FeatureValue = str | int | float | bool 에서
    # JSON 값 "10"은 유니온 멤버 str과 정확히 일치하므로 int로 coerce되지 않고 str로 보존된다.
    # 이 보존이 깨지면(유니온 정의 변경, strict/lax 설정 추가 등) 요청은 정상 매칭되어
    # 카운터가 영영 증가하지 않고 detection-only 계약과 메트릭이 죽은 코드가 된다.
    reranker = Reranker(
        model=CategoricalCodeModel(),
        feature_columns=("category_id",),
        categorical_categories={"category_id": (10, 20, 30)},
    )
    app = create_app(reranker=reranker)
    # 카운터는 모듈 전역이라 다른 테스트의 증가분이 누적된다. 절대값 대신 델타를 본다.
    labels = {"column": "category_id"}
    before = REGISTRY.get_sample_value("rerank_unseen_category_total", labels) or 0.0

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={
                "user_id": "user-1",
                "candidates": [
                    # 학습 카테고리는 int 10 — JSON str "10"은 매칭에 실패해야 한다.
                    {"video_id": "video-str", "features": {"category_id": "10"}},
                    {"video_id": "video-int", "features": {"category_id": 20}},
                ],
            },
        )

    after = REGISTRY.get_sample_value("rerank_unseen_category_total", labels) or 0.0

    assert response.status_code == 200
    # str "10" 후보 1건만 강등되어야 한다 — int 20은 학습 카테고리와 일치하므로 온전하다.
    assert after - before == 1.0
    # 강등된 후보는 NaN이 되어 category code -1(결측 sentinel)로 예측된다 — 매칭된 후보보다
    # 낮은 점수로 밀리는 조용한 품질 저하의 실체이며, 에러 없이 응답에 섞여 나간다.
    scores = {item["video_id"]: item["ctr_score"] for item in response.json()["items"]}
    assert scores["video-str"] == pytest.approx(-0.1)
    assert scores["video-int"] == pytest.approx(0.1)
    assert scores["video-str"] < scores["video-int"]


class WrongShapeModel:
    def predict_proba(self, features):
        return np.zeros((len(features), 3))


class ListReturningModel:
    def predict_proba(self, features):
        return [0.0] * len(features)


def test_rerank_raises_prediction_error_for_wrong_shaped_matrix() -> None:
    reranker = Reranker(
        model=WrongShapeModel(),
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )

    with pytest.raises(PredictionError) as excinfo:
        reranker.rerank([CandidateVideo(video_id="video-1", features={"ranking_signal": 0.5})])

    assert excinfo.value.reason == "Model returned an invalid probability matrix."


def test_rerank_raises_prediction_error_when_model_returns_list() -> None:
    reranker = Reranker(
        model=ListReturningModel(),
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )

    with pytest.raises(PredictionError):
        reranker.rerank([CandidateVideo(video_id="video-1", features={"ranking_signal": 0.5})])


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


def test_rerank_returns_422_when_candidate_missing_feature() -> None:
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
                    {"video_id": "video-1", "features": {"ranking_signal": 0.5}},
                    {"video_id": "video-2", "features": {"other": 1.0}},
                ],
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "Missing required model features: ranking_signal"}


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
        LocalModelSettings(
            model_path=model_path,
            feature_columns_path=feature_columns_path,
            categorical_columns_path=categorical_columns_path,
        )
    )

    # int 카테고리가 pydantic 검증을 통과하며 int로 보존되어야 한다.
    # str/float로 강제되면 서빙이 모든 categorical 값을 NaN으로 만든다.
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


def test_metrics_expose_candidate_count_buckets() -> None:
    reranker = Reranker(
        model=RankingModel(),
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )
    app = create_app(reranker=reranker)

    with TestClient(app) as client:
        client.post(
            "/rerank",
            json={
                "user_id": "user-1",
                "candidates": [
                    {"video_id": "video-1", "features": {"ranking_signal": 0.5}},
                ],
            },
        )
        metrics_response = client.get("/metrics")

    assert 'rerank_candidates_bucket{le="50.0"}' in metrics_response.text
    assert 'rerank_candidates_bucket{le="500.0"}' in metrics_response.text
