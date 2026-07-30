from __future__ import annotations

import itertools
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from src.experiments.schemas import (
    EventKind,
    ExperimentState,
    ExperimentVerdict,
    FinalReport,
    HypothesisSubmission,
    StatusUpdate,
)
from src.experiments.service import (
    ExperimentAlreadyFinalizedError,
    ExperimentService,
)
from src.experiments.store import (
    ExperimentNotFoundError,
    InvalidExperimentIdError,
    JsonlExperimentStore,
    resolve_store_dir,
)


@pytest.fixture
def service(tmp_path: Path) -> ExperimentService:
    """고정 시계·id로 결정론적 서비스를 만든다."""
    ticks = itertools.count()
    suffixes = itertools.count()

    def clock() -> datetime:
        return datetime(2026, 7, 30, 12, 0, next(ticks), tzinfo=UTC)

    def suffix() -> str:
        return f"{next(suffixes):08x}"

    return ExperimentService(
        JsonlExperimentStore(tmp_path), clock=clock, id_suffix_factory=suffix
    )


def _submission(title: str = "임베딩 교체 실험") -> HypothesisSubmission:
    return HypothesisSubmission(
        title=title,
        hypothesis="영상 임베딩을 교체하면 CTR AUC가 오른다",
        submitted_by="researcher@example.com",
        labels={"track": "embedding"},
    )


def test_submit_issues_id_and_starts_in_submitted_state(service: ExperimentService) -> None:
    accepted = service.submit(_submission())

    assert accepted.experiment_id == "exp_20260730_00000000"
    assert accepted.state is ExperimentState.SUBMITTED

    detail = service.get(accepted.experiment_id)
    assert detail.title == "임베딩 교체 실험"
    assert detail.state is ExperimentState.SUBMITTED
    assert detail.stage is None
    assert detail.report is None
    assert [event.kind for event in detail.events] == [EventKind.SUBMITTED]


def test_status_reports_move_to_running_and_keep_latest_stage(service: ExperimentService) -> None:
    experiment_id = service.submit(_submission()).experiment_id

    service.report_status(experiment_id, StatusUpdate(stage="피처 조립", progress=0.2))
    service.report_status(
        experiment_id,
        StatusUpdate(stage="학습", message="LightGBM 200 round", progress=0.6, metrics={"auc": 0.71}),
    )

    detail = service.get(experiment_id)
    assert detail.state is ExperimentState.RUNNING
    assert detail.stage == "학습"
    assert detail.progress == pytest.approx(0.6)
    assert [event.seq for event in detail.events] == [1, 2, 3]


def test_progress_may_go_backwards_because_retry_is_a_normal_path(
    service: ExperimentService,
) -> None:
    experiment_id = service.submit(_submission()).experiment_id

    service.report_status(experiment_id, StatusUpdate(stage="학습", progress=0.8))
    service.report_status(experiment_id, StatusUpdate(stage="피처 재조립", progress=0.3))

    assert service.get(experiment_id).progress == pytest.approx(0.3)


@pytest.mark.parametrize(
    ("verdict", "expected"),
    [
        (ExperimentVerdict.SUPPORTED, ExperimentState.SUCCEEDED),
        (ExperimentVerdict.REJECTED, ExperimentState.SUCCEEDED),
        (ExperimentVerdict.INCONCLUSIVE, ExperimentState.SUCCEEDED),
        (ExperimentVerdict.ERROR, ExperimentState.FAILED),
    ],
)
def test_report_verdict_maps_to_state_and_only_error_fails(
    service: ExperimentService, verdict: ExperimentVerdict, expected: ExperimentState
) -> None:
    experiment_id = service.submit(_submission()).experiment_id

    service.finalize(experiment_id, FinalReport(verdict=verdict, summary="결론"))

    detail = service.get(experiment_id)
    assert detail.state is expected
    assert detail.report is not None
    assert detail.report.verdict is verdict


def test_finalized_experiment_rejects_further_reports(service: ExperimentService) -> None:
    experiment_id = service.submit(_submission()).experiment_id
    service.finalize(
        experiment_id, FinalReport(verdict=ExperimentVerdict.SUPPORTED, summary="끝")
    )

    with pytest.raises(ExperimentAlreadyFinalizedError):
        service.report_status(experiment_id, StatusUpdate(stage="학습"))
    with pytest.raises(ExperimentAlreadyFinalizedError):
        service.finalize(
            experiment_id, FinalReport(verdict=ExperimentVerdict.REJECTED, summary="다시")
        )


def test_status_without_submission_is_not_found(service: ExperimentService) -> None:
    with pytest.raises(ExperimentNotFoundError):
        service.report_status("exp_20260730_deadbeef", StatusUpdate(stage="학습"))


def test_list_orders_newest_submission_first(service: ExperimentService) -> None:
    first = service.submit(_submission("첫 실험")).experiment_id
    second = service.submit(_submission("두번째 실험")).experiment_id
    service.report_status(second, StatusUpdate(stage="학습"))

    summaries = service.list()

    assert [item.experiment_id for item in summaries] == [second, first]
    assert summaries[0].state is ExperimentState.RUNNING
    assert summaries[0].event_count == 2


def test_events_survive_reload_because_store_is_append_only(tmp_path: Path) -> None:
    store = JsonlExperimentStore(tmp_path)
    service = ExperimentService(store)
    experiment_id = service.submit(_submission()).experiment_id
    service.report_status(experiment_id, StatusUpdate(stage="학습"))

    reloaded = ExperimentService(JsonlExperimentStore(tmp_path)).get(experiment_id)

    assert [event.kind for event in reloaded.events] == [EventKind.SUBMITTED, EventKind.STATUS]


@pytest.mark.parametrize(
    "experiment_id",
    ["../etc/passwd", "exp_2026_bad", "exp_20260730_XXXXXXXX", "exp_20260730_deadbee"],
)
def test_invalid_experiment_id_is_rejected_before_touching_disk(
    tmp_path: Path, experiment_id: str
) -> None:
    store = JsonlExperimentStore(tmp_path)

    with pytest.raises(InvalidExperimentIdError):
        store.read_events(experiment_id)


def test_store_dir_prefers_argument_then_env(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("AUTORESEARCH_EXPERIMENT_STORE_DIR", str(tmp_path / "from-env"))

    assert resolve_store_dir(tmp_path / "explicit") == tmp_path / "explicit"
    assert resolve_store_dir() == tmp_path / "from-env"


@pytest.mark.parametrize(
    "payload",
    [
        {"title": "", "hypothesis": "가설"},
        {"title": "제목", "hypothesis": ""},
        {"title": "제목", "hypothesis": "가설", "unknown": 1},
    ],
)
def test_submission_rejects_invalid_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        HypothesisSubmission.model_validate(payload)


@pytest.mark.parametrize("progress", [-0.1, 1.1])
def test_status_progress_must_be_a_ratio(progress: float) -> None:
    with pytest.raises(ValidationError):
        StatusUpdate(stage="학습", progress=progress)
