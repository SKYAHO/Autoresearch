import importlib.util
import sys
import types
from pathlib import Path


def _load_action_log_dag(monkeypatch, variable_values: dict[str, str]):
    """Airflow 없이 DAG helper를 검증하기 위해 최소 stub 모듈로 DAG 파일을 로드한다."""

    airflow = types.ModuleType("airflow")
    decorators = types.ModuleType("airflow.decorators")
    models = types.ModuleType("airflow.models")

    def dag(**_kwargs):
        def _decorate(_func):
            return lambda: None

        return _decorate

    def task(func):
        return func

    class Variable:
        @staticmethod
        def get(name, default_var=None):
            return variable_values.get(name, default_var)

    decorators.dag = dag
    decorators.task = task
    models.Variable = Variable
    monkeypatch.setitem(sys.modules, "airflow", airflow)
    monkeypatch.setitem(sys.modules, "airflow.decorators", decorators)
    monkeypatch.setitem(sys.modules, "airflow.models", models)

    path = Path(__file__).resolve().parents[2] / "dags" / "youtube_action_log_daily.py"
    spec = importlib.util.spec_from_file_location("_test_youtube_action_log_daily", path)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


def test_get_config_treats_blank_airflow_variable_as_missing(monkeypatch):
    module = _load_action_log_dag(
        monkeypatch,
        {"ACTION_LOG_OUTPUT_DIR": ""},
    )

    assert module._get_config("ACTION_LOG_OUTPUT_DIR", "bucket/data_lake/action_log") == (
        "bucket/data_lake/action_log"
    )


def test_get_config_uses_non_blank_airflow_variable(monkeypatch):
    module = _load_action_log_dag(
        monkeypatch,
        {"ACTION_LOG_OUTPUT_DIR": "custom/action_log"},
    )

    assert module._get_config("ACTION_LOG_OUTPUT_DIR", "bucket/data_lake/action_log") == (
        "custom/action_log"
    )
