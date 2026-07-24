"""registry(alias) 모델 소스 확장 단위 테스트 — 실 MLflow 미접속(stub)."""

from types import SimpleNamespace

import pytest

import src.serving.model_loader as model_loader
from src.serving.model_loader import (
    ModelConfigurationError,
    RegistryModelSettings,
    ResolvedModel,
    load_model_settings_from_environment,
    load_reranker_with_lineage,
)


class _SentinelReranker:
    """로더 위임 검증용 자리표시자 (Reranker 프로토콜 검사 없이 통과)."""


def test_environment_parses_registry_source(monkeypatch):
    monkeypatch.setenv("RERANK_MODEL_SOURCE", "registry")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
    monkeypatch.delenv("RERANK_REGISTRY_MODEL_NAME", raising=False)
    monkeypatch.delenv("RERANK_REGISTRY_ALIAS", raising=False)
    settings = load_model_settings_from_environment()
    assert settings == RegistryModelSettings(
        tracking_uri="http://mlflow:5000", model_name="ctr-model", alias="champion"
    )


def test_environment_registry_requires_tracking_uri(monkeypatch):
    monkeypatch.setenv("RERANK_MODEL_SOURCE", "registry")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    with pytest.raises(ModelConfigurationError):
        load_model_settings_from_environment()


def test_registry_resolves_alias_and_reuses_run_download(monkeypatch):
    calls = {}

    class _FakeClient:
        def get_model_version_by_alias(self, name, alias):
            calls["alias"] = (name, alias)
            return SimpleNamespace(run_id="run-abc", version=7)

    sentinel = _SentinelReranker()

    def _fake_load_mlflow_model(settings):
        calls["mlflow_settings"] = settings
        return sentinel

    monkeypatch.setattr(model_loader, "MlflowClient", lambda: _FakeClient())
    monkeypatch.setattr(model_loader, "load_mlflow_model", _fake_load_mlflow_model)

    resolved = load_reranker_with_lineage(
        RegistryModelSettings(
            tracking_uri="http://mlflow:5000", model_name="ctr-model", alias="champion"
        )
    )
    assert isinstance(resolved, ResolvedModel)
    assert resolved.reranker is sentinel
    assert (resolved.run_id, resolved.model_version) == ("run-abc", "7")
    assert calls["alias"] == ("ctr-model", "champion")
    assert calls["mlflow_settings"].run_id == "run-abc"
    assert calls["mlflow_settings"].tracking_uri == "http://mlflow:5000"


def test_registry_alias_failure_maps_to_artifact_error(monkeypatch):
    class _BrokenClient:
        def get_model_version_by_alias(self, name, alias):
            raise RuntimeError("registry unavailable")

    monkeypatch.setattr(model_loader, "MlflowClient", lambda: _BrokenClient())
    with pytest.raises(model_loader.ModelArtifactError):
        load_reranker_with_lineage(
            RegistryModelSettings(
                tracking_uri="http://mlflow:5000", model_name="ctr-model", alias="champion"
            )
        )


def test_lineage_for_mlflow_and_local_sources(monkeypatch, tmp_path):
    sentinel = _SentinelReranker()
    monkeypatch.setattr(model_loader, "load_mlflow_model", lambda s: sentinel)
    monkeypatch.setattr(model_loader, "load_local_model", lambda s: sentinel)

    from src.serving.model_loader import LocalModelSettings, MlflowModelSettings

    mlflow_resolved = load_reranker_with_lineage(
        MlflowModelSettings(tracking_uri="http://mlflow:5000", run_id="run-z")
    )
    assert (mlflow_resolved.run_id, mlflow_resolved.model_version) == ("run-z", None)

    local_resolved = load_reranker_with_lineage(
        LocalModelSettings(
            model_path=tmp_path / "m.joblib",
            feature_columns_path=tmp_path / "f.pkl",
            categorical_columns_path=tmp_path / "c.pkl",
        )
    )
    assert (local_resolved.run_id, local_resolved.model_version) == ("local", None)


# ── #302 calibration 페어링 fail-closed 검증 ──────────────────


class _PairingClient:
    """main / calibration 두 등록 모델을 name으로 분기해 resolve하는 가짜 client."""

    def __init__(self, calibration_main_run_id):
        self._cal_main = calibration_main_run_id

    def get_model_version_by_alias(self, name, alias):
        if name == "ctr-model":
            return SimpleNamespace(run_id="run-main", version=8, tags={})
        return SimpleNamespace(
            run_id="run-cal", version=3, tags={"main_run_id": self._cal_main}
        )


def _registry_settings():
    return RegistryModelSettings(
        tracking_uri="http://mlflow:5000",
        model_name="ctr-model",
        alias="champion",
        calibration_model_name="ctr-calibration-model",
        calibration_alias="champion",
    )


def test_pairing_mismatch_raises_model_artifact_error(monkeypatch):
    # calibration이 다른 main(run_id)을 가리키면 서빙 기동을 fail-closed로 막는다.
    monkeypatch.setattr(model_loader, "MlflowClient", lambda: _PairingClient("OTHER-run"))
    monkeypatch.setattr(model_loader, "load_mlflow_model", lambda s: _SentinelReranker())
    with pytest.raises(model_loader.ModelArtifactError, match="페어링"):
        load_reranker_with_lineage(_registry_settings())


def test_pairing_match_loads_and_threads_calibration_run_id(monkeypatch):
    # 짝이 맞으면 정상 로드하고 calibration run_id를 load_mlflow_model로 넘긴다.
    captured = {}
    monkeypatch.setattr(model_loader, "MlflowClient", lambda: _PairingClient("run-main"))

    def _fake(settings):
        captured["settings"] = settings
        return _SentinelReranker()

    monkeypatch.setattr(model_loader, "load_mlflow_model", _fake)
    resolved = load_reranker_with_lineage(_registry_settings())
    assert resolved.run_id == "run-main"
    assert captured["settings"].calibration_run_id == "run-cal"


def test_no_calibration_skips_pairing_and_threads_none(monkeypatch):
    # calibration_model_name 미지정(하위호환) → 페어링 검증 없이 calibration_run_id=None.
    captured = {}
    monkeypatch.setattr(model_loader, "MlflowClient", lambda: _PairingClient("irrelevant"))

    def _fake(settings):
        captured["settings"] = settings
        return _SentinelReranker()

    monkeypatch.setattr(model_loader, "load_mlflow_model", _fake)
    load_reranker_with_lineage(
        RegistryModelSettings(
            tracking_uri="http://mlflow:5000", model_name="ctr-model", alias="champion"
        )
    )
    assert captured["settings"].calibration_run_id is None
