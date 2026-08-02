from __future__ import annotations

import math
import json
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


@pytest.fixture(autouse=True)
def _promote_tracking_uri(monkeypatch):
    """승격 경로가 요구하는 tracking URI를 테스트가 스스로 설정한다.

    `promote.main()`은 `MLFLOW_TRACKING_URI`가 비면 fail-fast한다(#406). 이 파일의
    테스트들은 그 값을 설정하지 않았는데도 전체 스위트에서는 통과했다 — 앞서 실행된
    학습 테스트가 `mlflow.set_tracking_uri()`를 부르면 **MLflow가 os.environ에 값을
    써서** 뒤 테스트로 새기 때문이다(실측 확인).

    그래서 단독 실행하면 23건이 무더기로 실패했다. 실행 순서에 기대는 통과는 통과가
    아니므로 여기서 명시적으로 준다.
    """
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow-test:5000")


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
        self, *, main_versions=None, runs=None, calibration_runs=None, manifest_runs=None,
        invalid_manifest_runs=None, list_artifacts_error=False
    ):
        self.main_versions = main_versions or []
        self.runs = runs or {}
        self.calibration_runs = set(calibration_runs or [])
        self.manifest_runs = set(self.runs if manifest_runs is None else manifest_runs)
        self.invalid_manifest_runs = set(invalid_manifest_runs or [])
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
        if run_id in self.manifest_runs | self.invalid_manifest_runs and path == "manifest":
            return [SimpleNamespace(path="manifest/manifest.json")]
        return []

    def download_artifacts(self, run_id, path, dst_path):
        target = Path(dst_path) / "manifest.json"
        payload = {"contract_version": "wrong"}
        if run_id in self.manifest_runs:
            payload = {
                "contract_version": "ctr-model-package-v1",
                "feature_service": "ctr_training_v1",
                "sampling_rate": 1.0,
                "artifacts": {
                    "model_onnx": {"path": "model_onnx", "entrypoint": "model.onnx", "sha256": "a" * 64},
                    "feature_columns": {"path": "features/feature_columns.json", "sha256": "a" * 64},
                    "categorical_columns": {"path": "features/categorical_columns.json", "sha256": "a" * 64},
                    "calibration": None,
                },
            }
        target.write_text(json.dumps(payload), encoding="utf-8")
        return str(target)

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
    client = _PromoteClient(main_versions=[v1], runs={"run-1": {"val_roc_auc": 0.70}}, manifest_runs=["run-1"])
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.PROMOTED
    assert result.reason_code is PromotionReasonCode.FIRST_CHAMPION
    assert result.candidate_version == "1"
    assert result.champion_version is None
    assert result.candidate_metric == 0.70
    assert client.set_alias_calls == [(MODEL_NAME, "champion", "1")]


@pytest.mark.parametrize("invalid", [False, True])
def test_main_rejects_candidate_without_valid_manifest(monkeypatch, invalid):
    candidate = _version("1", run_id="run-1")
    client = _PromoteClient(
        main_versions=[candidate], runs={"run-1": {"val_roc_auc": 0.70}},
        manifest_runs=[],
        invalid_manifest_runs=["run-1"] if invalid else [],
    )
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.REJECTED
    assert result.reason_code is PromotionReasonCode.MANIFEST_ARTIFACT_INVALID
    assert client.set_alias_calls == []


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


@pytest.mark.parametrize("metric", [math.nan, math.inf, -math.inf])
def test_main_rejects_non_finite_candidate_metric_before_alias_update(
    monkeypatch, metric
):
    candidate = _version("1", run_id="run-1")
    client = _PromoteClient(
        main_versions=[candidate],
        runs={"run-1": {"val_roc_auc": metric}},
    )
    _patch_client(monkeypatch, client)

    with pytest.raises(PromotionExecutionError) as exc_info:
        promote.main(MODEL_NAME, "champion")

    assert exc_info.value.reason_code is PromotionReasonCode.METRIC_MISSING
    assert exc_info.value.candidate_version == "1"
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
    assert result.legacy_message is not None
    assert "sampling_rate=0.5" in result.legacy_message
    assert "run(run-4)" in result.legacy_message
    assert "calibration/calibration.json" in result.legacy_message
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


@pytest.mark.parametrize("metric", [math.nan, math.inf, -math.inf])
def test_main_rejects_non_finite_champion_metric_before_alias_update(
    monkeypatch, metric
):
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4")
    client = _PromoteClient(
        main_versions=[champion, candidate],
        runs={
            "run-3": {"val_roc_auc": metric},
            "run-4": {"val_roc_auc": 0.80},
        },
    )
    _patch_client(monkeypatch, client)

    with pytest.raises(PromotionExecutionError) as exc_info:
        promote.main(MODEL_NAME, "champion")

    assert exc_info.value.reason_code is PromotionReasonCode.METRIC_MISSING
    assert exc_info.value.candidate_metric == 0.80
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


# --- 실험 모델 승격 차단 (#405) ---


def test_experiment_feature_version_never_becomes_candidate(monkeypatch):
    """실험 피처로 학습한 버전은 지표가 아무리 좋아도 후보가 되지 않는다.

    prod 계약에 없는 입력으로 학습된 모델이라 서빙이 그 피처를 만들어낼 수 없다.
    거부가 아니라 **후보 선택에서 제외**한다 — 거부만 하면 그 버전이 후보 자리를
    차지해 앞의 정상 후보까지 막힌다(#405 리뷰 1).

    여기서는 champion(v3) 말고 승격 가능한 버전이 없으므로 champion 유지로 끝난다.
    """
    champion = _version("3", aliases=["champion"], run_id="run-3")
    experiment = _version(
        "4", run_id="run-4", tags={"experiment_features": "views_per_day"}
    )
    client = _PromoteClient(
        main_versions=[champion, experiment],
        runs={"run-3": {"val_roc_auc": 0.75}, "run-4": {"val_roc_auc": 0.99}},
    )
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.NO_CANDIDATE
    assert result.reason_code is PromotionReasonCode.ALREADY_CHAMPION
    assert result.candidate_version == "3"
    # 지표 0.99짜리 실험 버전이 champion을 가져가지 않는다.
    assert client.set_alias_calls == []


def test_main_promotes_when_experiment_tag_is_absent(monkeypatch):
    """일반 후보는 태그가 없으므로 기존 경로 그대로 승격된다(#405 회귀 방지)."""
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4", tags={"sampling_rate": "1.0"})
    client = _PromoteClient(
        main_versions=[champion, candidate],
        runs={"run-3": {"val_roc_auc": 0.75}, "run-4": {"val_roc_auc": 0.80}},
    )
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.PROMOTED
    assert client.set_alias_calls == [(MODEL_NAME, "champion", "4")]


def test_main_ignores_empty_experiment_tag(monkeypatch):
    """빈 문자열 태그는 실험 표식으로 보지 않는다."""
    candidate = _version("1", run_id="run-1", tags={"experiment_features": ""})
    client = _PromoteClient(main_versions=[candidate], runs={"run-1": {"val_roc_auc": 0.70}})
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.PROMOTED


def test_main_rejects_experiment_namespace_model_name(monkeypatch):
    """실험 네임스페이스 이름으로 승격을 부르면 registry 조회 전에 거부한다(#406).

    #405의 후보 제외와 층이 다르다 — 저쪽은 prod 이름 안에 섞인 실험 **버전**을
    거르고, 이쪽은 실험 전용 registry **이름**으로 부른 호출 자체를 막는다.
    """
    client = _PromoteClient(main_versions=[])
    _patch_client(monkeypatch, client)

    result = promote.main("ctr-model-exp-views-per-day", "champion")

    # #405의 후보 제외와 같은 분류 — 게이트 미달이 아니라 심사 대상 부재다.
    assert result.outcome is PromotionOutcome.NO_CANDIDATE
    assert result.reason_code is PromotionReasonCode.EXPERIMENT_MODEL
    assert client.set_alias_calls == []


def test_experiment_version_does_not_block_earlier_prod_candidate(monkeypatch):
    """실험 버전이 번호상 최신이어도 그 앞의 prod 후보가 승격된다(#405 리뷰 1).

    예전에는 후보를 버전 번호 최대값 하나로 골라 거부만 했다. 그러면 실험이
    만든 v11이 후보를 차지하고 정상 후보 v10은 영원히 승격되지 못해, 실험을
    돌린 날의 champion 갱신이 조용히 유실됐다.
    """
    champion = _version("9", aliases=["champion"], run_id="run-9")
    prod_candidate = _version("10", run_id="run-10")
    experiment = _version(
        "11", run_id="run-11", tags={"experiment_features": "views_per_day"}
    )
    client = _PromoteClient(
        main_versions=[champion, prod_candidate, experiment],
        runs={"run-9": {"val_roc_auc": 0.75}, "run-10": {"val_roc_auc": 0.80}},
    )
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.PROMOTED
    assert result.candidate_version == "10"
    assert client.set_alias_calls == [(MODEL_NAME, "champion", "10")]


def test_all_experiment_versions_report_no_candidate(monkeypatch):
    """등록된 게 전부 실험 모델이면 '게이트 미달'이 아니라 '후보 없음'이다.

    일일 DAG의 알람 해석이 어긋나지 않도록 REJECTED가 아닌 NO_CANDIDATE로 분류한다.
    """
    experiment = _version(
        "1", run_id="run-1", tags={"experiment_features": "views_per_day"}
    )
    client = _PromoteClient(main_versions=[experiment], runs={"run-1": {"val_roc_auc": 0.9}})
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion")

    assert result.outcome is PromotionOutcome.NO_CANDIDATE
    assert result.reason_code is PromotionReasonCode.EXPERIMENT_MODEL
    assert client.set_alias_calls == []
