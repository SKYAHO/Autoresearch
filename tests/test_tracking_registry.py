import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
from mlflow.exceptions import MlflowException

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.tracking import registry  # noqa: E402


def test_register_model_calls_mlflow_register_model(monkeypatch):
    fake_model_version = MagicMock(version="3")
    fake_register_model = MagicMock(return_value=fake_model_version)
    monkeypatch.setattr(registry.mlflow, "register_model", fake_register_model)

    result = registry.register_model("runs:/abc123/model", "ctr-model")

    fake_register_model.assert_called_once_with("runs:/abc123/model", "ctr-model")
    assert result == "3"


def test_register_model_sets_tags_via_client(monkeypatch):
    fake_model_version = MagicMock(version="4")
    monkeypatch.setattr(
        registry.mlflow, "register_model", MagicMock(return_value=fake_model_version)
    )
    fake_client = MagicMock()
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    registry.register_model(
        "runs:/abc123/model", "ctr-model", tags={"val_roc_auc": "0.85"}
    )

    fake_client.set_model_version_tag.assert_called_once_with(
        "ctr-model", "4", "val_roc_auc", "0.85"
    )


def test_register_model_without_tags_skips_client(monkeypatch):
    fake_model_version = MagicMock(version="1")
    monkeypatch.setattr(
        registry.mlflow, "register_model", MagicMock(return_value=fake_model_version)
    )
    fake_client_cls = MagicMock()
    monkeypatch.setattr(registry, "MlflowClient", fake_client_cls)

    registry.register_model("runs:/abc123/model", "ctr-model")

    fake_client_cls.assert_not_called()


def test_get_model_versions_returns_empty_list_when_no_versions(monkeypatch):
    fake_client = MagicMock()
    fake_client.search_model_versions.return_value = []
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    assert registry.get_model_versions("ctr-model") == []


def test_get_latest_version_picks_highest_version_number(monkeypatch):
    fake_client = MagicMock()
    fake_client.search_model_versions.return_value = [
        MagicMock(version="2"),
        MagicMock(version="10"),
        MagicMock(version="1"),
    ]
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    assert registry.get_latest_version("ctr-model") == "10"


def test_get_latest_version_returns_none_when_no_versions(monkeypatch):
    fake_client = MagicMock()
    fake_client.search_model_versions.return_value = []
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    assert registry.get_latest_version("ctr-model") is None


def test_get_model_metrics_by_alias_returns_none_when_alias_missing(monkeypatch):
    fake_client = MagicMock()
    fake_client.get_model_version_by_alias.side_effect = MlflowException(
        "Registered model alias champion not found"
    )
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    assert registry.get_model_metrics_by_alias("ctr-model") is None


def test_get_model_metrics_by_alias_reraises_unexpected_errors(monkeypatch):
    fake_client = MagicMock()
    fake_client.get_model_version_by_alias.side_effect = MlflowException(
        "connection refused"
    )
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    with pytest.raises(MlflowException):
        registry.get_model_metrics_by_alias("ctr-model")


def _fake_client_with_version_tags(tags: dict):
    fake_version = MagicMock()
    fake_version.tags = tags
    fake_client = MagicMock()
    fake_client.get_model_version.return_value = fake_version
    return fake_client


def test_set_champion_alias_rejects_downsampled_model_when_not_ready(monkeypatch):
    # #300 순서 가드: downsampling 모델(sampling_rate<1.0)은 서빙 보정 미준비 시
    # champion 승격 거부.
    monkeypatch.delenv("CTR_SERVING_CALIBRATION_READY", raising=False)
    fake_client = _fake_client_with_version_tags({"sampling_rate": "0.5"})
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    with pytest.raises(ValueError, match="champion 승격을 거부"):
        registry.set_model_alias("ctr-model", "champion", "7")
    fake_client.set_registered_model_alias.assert_not_called()


def test_set_champion_alias_allows_downsampled_model_when_calibration_ready(monkeypatch):
    # #302가 서빙 보정을 편입해 플래그를 켜면 승격 허용.
    monkeypatch.setenv("CTR_SERVING_CALIBRATION_READY", "true")
    fake_client = _fake_client_with_version_tags({"sampling_rate": "0.5"})
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    registry.set_model_alias("ctr-model", "champion", "7")
    fake_client.set_registered_model_alias.assert_called_once()


def test_set_champion_alias_allows_full_rate_model(monkeypatch):
    # sampling_rate=1.0(또는 tag 없음, 기존 v6류)은 downsampling 모델이 아니라 정상 승격.
    monkeypatch.delenv("CTR_SERVING_CALIBRATION_READY", raising=False)
    fake_client = _fake_client_with_version_tags({})  # tag 없음 → 1.0 간주
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    registry.set_model_alias("ctr-model", "champion", "6")
    fake_client.set_registered_model_alias.assert_called_once()


def test_set_non_champion_alias_skips_gate(monkeypatch):
    # champion 외 alias(challenger/rollback)는 게이트 미적용.
    monkeypatch.delenv("CTR_SERVING_CALIBRATION_READY", raising=False)
    fake_client = _fake_client_with_version_tags({"sampling_rate": "0.5"})
    monkeypatch.setattr(registry, "MlflowClient", MagicMock(return_value=fake_client))

    registry.set_model_alias("ctr-model", "challenger", "7")
    fake_client.set_registered_model_alias.assert_called_once()
    fake_client.get_model_version.assert_not_called()
