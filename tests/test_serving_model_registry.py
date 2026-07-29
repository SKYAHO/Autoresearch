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


# ── #390 calibration run_id 종속(같은 run) ──────────────────


class _MainVersionClient:
    """main alias만 resolve하는 가짜 client(#390 — calibration은 별도 등록하지 않는다).

    main_sampling_rate=None이면 sampling_rate tag 없음(non-downsampling, 예 v6).
    """

    def __init__(self, main_sampling_rate="0.5"):
        self._main_sr = main_sampling_rate

    def get_model_version_by_alias(self, name, alias):
        tags = {} if self._main_sr is None else {"sampling_rate": self._main_sr}
        return SimpleNamespace(run_id="run-main", version=8, tags=tags)


def _registry_settings():
    return RegistryModelSettings(
        tracking_uri="http://mlflow:5000", model_name="ctr-model", alias="champion"
    )


def _capture_load(monkeypatch, captured):
    def _fake(settings):
        captured["settings"] = settings
        return _SentinelReranker()

    monkeypatch.setattr(model_loader, "load_mlflow_model", _fake)


def test_downsampling_main_threads_same_run_calibration(monkeypatch):
    # downsampling main이면 calibration을 같은 run(run_id)에서 읽도록 main run_id를 넘긴다.
    captured = {}
    monkeypatch.setattr(model_loader, "MlflowClient", lambda: _MainVersionClient("0.5"))
    _capture_load(monkeypatch, captured)

    resolved = load_reranker_with_lineage(_registry_settings())

    assert resolved.run_id == "run-main"
    assert captured["settings"].calibration_run_id == "run-main"


def test_non_downsampling_main_threads_none(monkeypatch):
    # non-downsampling main(sampling_rate>=1.0)이면 보정 불필요 → calibration_run_id=None.
    captured = {}
    monkeypatch.setattr(model_loader, "MlflowClient", lambda: _MainVersionClient("1.0"))
    _capture_load(monkeypatch, captured)

    load_reranker_with_lineage(_registry_settings())

    assert captured["settings"].calibration_run_id is None


def test_missing_sampling_rate_tag_threads_none(monkeypatch):
    # sampling_rate tag 없는 기존 모델(v6)은 non-downsampling으로 간주 → 항등(None).
    captured = {}
    monkeypatch.setattr(
        model_loader, "MlflowClient", lambda: _MainVersionClient(main_sampling_rate=None)
    )
    _capture_load(monkeypatch, captured)

    load_reranker_with_lineage(_registry_settings())

    assert captured["settings"].calibration_run_id is None
