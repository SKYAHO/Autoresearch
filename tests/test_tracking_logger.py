import sys
from pathlib import Path
from unittest.mock import MagicMock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.tracking import logger  # noqa: E402


def test_log_onnx_model_calls_mlflow_onnx_log_model(monkeypatch):
    fake_onnx_module = MagicMock()
    monkeypatch.setitem(sys.modules, "mlflow.onnx", fake_onnx_module)

    sentinel_model = object()
    logger.log_onnx_model(sentinel_model, artifact_path="custom_onnx_path")

    fake_onnx_module.log_model.assert_called_once_with(
        sentinel_model, artifact_path="custom_onnx_path"
    )
