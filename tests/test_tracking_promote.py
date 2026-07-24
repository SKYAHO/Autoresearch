from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.tracking import promote, registry  # noqa: E402

MODEL_NAME = "ctr-model"
CALIBRATION_MODEL_NAME = "ctr-calibration-model"


def _version(version, *, aliases=None, run_id=None, tags=None):
    return SimpleNamespace(
        version=version,
        aliases=aliases or [],
        run_id=run_id or f"run-v{version}",
        tags=tags or {},
        creation_timestamp=0,
    )


class _PromoteClient:
    """model_name(main)과 calibration_model_name을 name으로 구분하는 가짜 client.

    tests/test_serving_model_registry.py의 _PairingClient와 같은 패턴 —
    실제 MLflow 서버 없이 registry.py/promote.py가 호출하는 MlflowClient
    메서드 표면만 흉내낸다.
    """

    def __init__(self, *, main_versions=None, calibration_versions=None, runs=None):
        self.main_versions = main_versions or []
        self.calibration_versions = calibration_versions or []
        self.runs = runs or {}
        self.set_alias_calls: list[tuple[str, str, str]] = []

    def _versions_for(self, name):
        return self.main_versions if name == MODEL_NAME else self.calibration_versions

    def search_model_versions(self, filter_string):
        name = MODEL_NAME if MODEL_NAME in filter_string else CALIBRATION_MODEL_NAME
        return self._versions_for(name)

    def get_model_version(self, name, version):
        for v in self._versions_for(name):
            if v.version == str(version):
                return v
        raise registry.MlflowException(f"version not found: {name} v{version}")

    def get_model_version_by_alias(self, name, alias):
        for v in self._versions_for(name):
            if alias in v.aliases:
                return v
        raise registry.MlflowException(f"Registered model alias {alias} not found")

    def get_run(self, run_id):
        return SimpleNamespace(data=SimpleNamespace(metrics=self.runs.get(run_id, {})))

    def set_registered_model_alias(self, name, alias, version):
        self.set_alias_calls.append((name, alias, str(version)))


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(registry, "MlflowClient", lambda: client)
    monkeypatch.setattr(promote, "MlflowClient", lambda: client)


def test_main_returns_none_when_no_versions_registered(monkeypatch):
    client = _PromoteClient(main_versions=[])
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)

    assert result is None
    assert client.set_alias_calls == []


def test_main_returns_none_when_latest_is_already_champion(monkeypatch):
    v5 = _version("5", aliases=["champion"], run_id="run-5")
    client = _PromoteClient(main_versions=[v5], runs={"run-5": {"val_roc_auc": 0.80}})
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)

    assert result is None
    assert client.set_alias_calls == []
