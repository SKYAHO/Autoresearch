from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.tracking import promote, registry  # noqa: E402
from src.tracking.promotion_result import (  # noqa: E402
    PromotionExecutionError,
    PromotionOutcome,
    PromotionReasonCode,
)

MODEL_NAME = "ctr-model"


def _version(version, *, aliases=None, run_id=None, tags=None):
    return SimpleNamespace(
        version=version,
        aliases=aliases or [],
        run_id=run_id or f"run-v{version}",
        tags=tags or {},
        creation_timestamp=0,
    )


class _PromoteClient:
    """실제 MLflow 서버 없이 registry.py/promote.py가 호출하는 MlflowClient 메서드
    표면만 흉내내는 가짜 client(#390 — calibration은 별도 등록하지 않는다).

    calibration_runs: calibration 아티팩트가 있는 run_id 집합(게이트2용).
    list_artifacts_error=True면 아티팩트 스토어 접근 실패(인프라 오류)를 흉내낸다.
    """

    def __init__(
        self, *, main_versions=None, runs=None, calibration_runs=None, list_artifacts_error=False
    ):
        self.main_versions = main_versions or []
        self.runs = runs or {}
        self.calibration_runs = set(calibration_runs or [])
        self.list_artifacts_error = list_artifacts_error
        self.set_alias_calls: list[tuple[str, str, str]] = []

    def search_model_versions(self, filter_string):
        return self.main_versions

    def get_model_version(self, name, version):
        for v in self.main_versions:
            if v.version == str(version):
                return v
        raise registry.MlflowException(f"version not found: {name} v{version}")

    def get_model_version_by_alias(self, name, alias):
        for v in self.main_versions:
            if alias in v.aliases:
                return v
        raise registry.MlflowException(f"Registered model alias {alias} not found")

    def get_run(self, run_id):
        return SimpleNamespace(data=SimpleNamespace(metrics=self.runs.get(run_id, {})))

    def list_artifacts(self, run_id, path):
        if self.list_artifacts_error:
            raise RuntimeError("artifact store unreachable")
        if run_id in self.calibration_runs and path == "calibration":
            return [SimpleNamespace(path="calibration/calibration.json")]
        return []

    def set_registered_model_alias(self, name, alias, version):
        self.set_alias_calls.append((name, alias, str(version)))


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(registry, "MlflowClient", lambda: client)
    monkeypatch.setattr(promote, "MlflowClient", lambda: client)


def test_main_returns_no_candidate_when_no_versions_registered(monkeypatch):
    client = _PromoteClient(main_versions=[])
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.NO_CANDIDATE
    assert result.reason_code is PromotionReasonCode.REGISTRY_EMPTY
    assert result.candidate_version is None
    assert client.set_alias_calls == []


def test_main_returns_no_candidate_when_latest_is_already_champion(monkeypatch):
    v5 = _version("5", aliases=["champion"], run_id="run-5")
    client = _PromoteClient(main_versions=[v5], runs={"run-5": {"val_roc_auc": 0.80}})
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.NO_CANDIDATE
    assert result.reason_code is PromotionReasonCode.ALREADY_CHAMPION
    assert result.candidate_version == "5"
    assert result.champion_version == "5"
    assert client.set_alias_calls == []


def test_main_promotes_when_no_champion_exists_bootstrap(monkeypatch):
    # champion alias가 아직 없으면 비교 대상이 없어 게이트 1을 자동 통과한다.
    v1 = _version("1", run_id="run-1")
    client = _PromoteClient(main_versions=[v1], runs={"run-1": {"val_roc_auc": 0.70}})
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.PROMOTED
    assert result.reason_code is PromotionReasonCode.FIRST_CHAMPION
    assert result.candidate_version == "1"
    assert result.champion_version is None
    assert result.candidate_metric == 0.70
    assert client.set_alias_calls == [(MODEL_NAME, "champion", "1")]


def test_main_promotes_when_candidate_metric_is_better(monkeypatch):
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4")
    client = _PromoteClient(
        main_versions=[champion, candidate],
        runs={"run-3": {"val_roc_auc": 0.75}, "run-4": {"val_roc_auc": 0.80}},
    )
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.PROMOTED
    assert result.reason_code is PromotionReasonCode.METRIC_NOT_DEGRADED
    assert result.candidate_version == "4"
    assert result.champion_version == "3"
    assert result.candidate_metric == 0.80
    assert result.champion_metric == 0.75
    assert client.set_alias_calls == [(MODEL_NAME, "champion", "4")]


def test_main_rejects_when_candidate_metric_is_worse(monkeypatch):
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4")
    client = _PromoteClient(
        main_versions=[champion, candidate],
        runs={"run-3": {"val_roc_auc": 0.80}, "run-4": {"val_roc_auc": 0.70}},
    )
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.REJECTED
    assert result.reason_code is PromotionReasonCode.METRIC_BELOW_CHAMPION
    assert result.candidate_version == "4"
    assert result.champion_version == "3"
    assert client.set_alias_calls == []


def test_main_raises_typed_error_when_candidate_metric_missing(monkeypatch):
    candidate = _version("1", run_id="run-1")
    client = _PromoteClient(main_versions=[candidate], runs={"run-1": {}})
    _patch_client(monkeypatch, client)

    with pytest.raises(PromotionExecutionError) as exc_info:
        promote.main(MODEL_NAME, "champion")
    assert exc_info.value.reason_code is PromotionReasonCode.METRIC_MISSING
    assert client.set_alias_calls == []


def test_main_rejects_downsampling_candidate_without_calibration_artifact(monkeypatch):
    # downsampling 후보인데 같은 run에 calibration 아티팩트가 없으면 게이트2로 거부한다(#390).
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4", tags={"sampling_rate": "0.5"})
    client = _PromoteClient(
        main_versions=[champion, candidate],
        calibration_runs=[],  # run-4에 calibration 아티팩트 없음
        runs={"run-3": {"val_roc_auc": 0.70}, "run-4": {"val_roc_auc": 0.80}},
    )
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.REJECTED
    assert (
        result.reason_code
        is PromotionReasonCode.CALIBRATION_ARTIFACT_MISSING
    )
    assert client.set_alias_calls == []


def test_main_promotes_downsampling_candidate_with_calibration_artifact(monkeypatch):
    # downsampling 후보 run에 calibration 아티팩트가 있으면 게이트2 통과, main alias만 이동한다.
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4", tags={"sampling_rate": "0.5"})
    client = _PromoteClient(
        main_versions=[champion, candidate],
        calibration_runs=["run-4"],
        runs={"run-3": {"val_roc_auc": 0.70}, "run-4": {"val_roc_auc": 0.80}},
    )
    # set_model_alias의 #300 순서 가드(CTR_SERVING_CALIBRATION_READY)를 통과시켜야
    # 게이트2 이후의 실제 alias 이동까지 검증할 수 있다.
    monkeypatch.setenv("CTR_SERVING_CALIBRATION_READY", "true")
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.PROMOTED
    assert result.reason_code is PromotionReasonCode.METRIC_NOT_DEGRADED
    assert result.candidate_version == "4"
    # 승격 기준은 main 하나뿐 — calibration alias는 이동하지 않는다(#390).
    assert client.set_alias_calls == [(MODEL_NAME, "champion", "4")]


def test_main_downsampling_artifact_store_error_is_typed(monkeypatch):
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4", tags={"sampling_rate": "0.5"})
    client = _PromoteClient(
        main_versions=[champion, candidate],
        runs={"run-3": {"val_roc_auc": 0.70}, "run-4": {"val_roc_auc": 0.80}},
        list_artifacts_error=True,
    )
    _patch_client(monkeypatch, client)

    with pytest.raises(PromotionExecutionError) as exc_info:
        promote.main(MODEL_NAME, "champion")
    assert exc_info.value.reason_code is PromotionReasonCode.ARTIFACT_LOOKUP_FAILED
    assert client.set_alias_calls == []


def test_main_promotes_when_candidate_metric_equals_champion(monkeypatch):
    # 게이트1은 "이상"(>=)이 기준이므로 동률이면 통과해야 한다.
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4")
    client = _PromoteClient(
        main_versions=[champion, candidate],
        runs={"run-3": {"val_roc_auc": 0.80}, "run-4": {"val_roc_auc": 0.80}},
    )
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.PROMOTED
    assert result.reason_code is PromotionReasonCode.METRIC_NOT_DEGRADED
    assert client.set_alias_calls == [(MODEL_NAME, "champion", "4")]


def test_main_raises_typed_error_when_champion_metric_missing(monkeypatch):
    # champion run에 val_roc_auc 자체가 없으면(예: 과거 수동 승격) 비교 불가를
    # 자동 통과로 처리하지 않고 fail-closed로 거부한다(PR #343 리뷰 반영).
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4")
    client = _PromoteClient(
        main_versions=[champion, candidate],
        runs={"run-3": {}, "run-4": {"val_roc_auc": 0.80}},
    )
    _patch_client(monkeypatch, client)

    with pytest.raises(PromotionExecutionError) as exc_info:
        promote.main(MODEL_NAME, "champion")
    assert exc_info.value.reason_code is PromotionReasonCode.METRIC_MISSING
    assert client.set_alias_calls == []


def test_main_rejects_when_serving_calibration_is_not_ready(monkeypatch):
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4", tags={"sampling_rate": "0.5"})
    client = _PromoteClient(
        main_versions=[champion, candidate],
        calibration_runs=["run-4"],
        runs={"run-3": {"val_roc_auc": 0.70}, "run-4": {"val_roc_auc": 0.80}},
    )
    monkeypatch.delenv("CTR_SERVING_CALIBRATION_READY", raising=False)
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.REJECTED
    assert (
        result.reason_code
        is PromotionReasonCode.SERVING_CALIBRATION_NOT_READY
    )
    assert client.set_alias_calls == []
