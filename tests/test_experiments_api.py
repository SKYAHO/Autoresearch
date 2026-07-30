from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from src.experiments.api import create_app, render_dashboard
from src.experiments.service import ExperimentService
from src.experiments.store import JsonlExperimentStore


@pytest.fixture
def client(tmp_path: Path) -> TestClient:
    ticks = itertools.count()
    suffixes = itertools.count()

    def clock() -> datetime:
        return datetime(2026, 7, 30, 12, 0, next(ticks), tzinfo=UTC)

    def suffix() -> str:
        return f"{next(suffixes):08x}"

    service = ExperimentService(
        JsonlExperimentStore(tmp_path), clock=clock, id_suffix_factory=suffix
    )
    return TestClient(create_app(service))


SUBMISSION = {"title": "임베딩 교체", "hypothesis": "AUC가 오른다"}


def test_full_flow_submit_status_report(client: TestClient) -> None:
    created = client.post("/experiments", json=SUBMISSION)
    assert created.status_code == 201
    experiment_id = created.json()["experiment_id"]
    assert created.json()["state"] == "submitted"

    status_response = client.post(
        f"/experiments/{experiment_id}/status",
        json={"stage": "학습", "progress": 0.5, "metrics": {"auc": 0.7}},
    )
    assert status_response.status_code == 200
    assert status_response.json()["kind"] == "status"

    report_response = client.post(
        f"/experiments/{experiment_id}/report",
        json={"verdict": "supported", "summary": "champion 대비 +2%", "artifact_refs": ["runs:/abc"]},
    )
    assert report_response.status_code == 200

    detail = client.get(f"/experiments/{experiment_id}").json()
    assert detail["state"] == "succeeded"
    assert detail["stage"] == "학습"
    assert detail["report"]["artifact_refs"] == ["runs:/abc"]
    assert [event["seq"] for event in detail["events"]] == [1, 2, 3]


def test_unknown_experiment_returns_404(client: TestClient) -> None:
    response = client.get("/experiments/exp_20260730_deadbeef")

    assert response.status_code == 404


def test_malformed_experiment_id_returns_400_not_500(client: TestClient) -> None:
    response = client.get("/experiments/exp_20260730_zzzzzzzz")

    assert response.status_code == 400


def test_report_after_finalize_returns_409(client: TestClient) -> None:
    experiment_id = client.post("/experiments", json=SUBMISSION).json()["experiment_id"]
    client.post(
        f"/experiments/{experiment_id}/report", json={"verdict": "rejected", "summary": "기각"}
    )

    conflict = client.post(f"/experiments/{experiment_id}/status", json={"stage": "학습"})

    assert conflict.status_code == 409


def test_unknown_field_in_status_returns_422(client: TestClient) -> None:
    experiment_id = client.post("/experiments", json=SUBMISSION).json()["experiment_id"]

    response = client.post(
        f"/experiments/{experiment_id}/status", json={"stage": "학습", "typo": 1}
    )

    assert response.status_code == 422


def test_list_endpoint_returns_summaries(client: TestClient) -> None:
    client.post("/experiments", json=SUBMISSION)
    client.post("/experiments", json={"title": "두번째", "hypothesis": "가설"})

    listed = client.get("/experiments").json()

    assert [item["title"] for item in listed] == ["두번째", "임베딩 교체"]


def test_dashboard_renders_rows(client: TestClient) -> None:
    experiment_id = client.post("/experiments", json=SUBMISSION).json()["experiment_id"]
    client.post(f"/experiments/{experiment_id}/status", json={"stage": "학습", "progress": 0.42})

    page = client.get("/experiments/ui/dashboard")

    assert page.status_code == 200
    assert experiment_id in page.text
    assert "42%" in page.text


def test_dashboard_escapes_user_supplied_text(client: TestClient) -> None:
    client.post(
        "/experiments",
        json={"title": "<script>alert(1)</script>", "hypothesis": "가설"},
    )

    page = client.get("/experiments/ui/dashboard")

    assert "<script>alert(1)</script>" not in page.text
    assert "&lt;script&gt;" in page.text


def test_dashboard_without_experiments_gives_guidance() -> None:
    assert "가설을 제출" in render_dashboard([])
