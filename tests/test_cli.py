from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src import cli  # noqa: E402


def test_run_pipeline_forwards_dates_to_build_features(monkeypatch):
    build_features_call = {}
    # build-features 성공 뒤 lineage가 GCS_REGISTRY_PATH를 필수로 읽는다(#359 C2, 무조건 기록).
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", lambda **kw: build_features_call.update(kw))
    monkeypatch.setattr(cli.train, "main", lambda **kw: None)
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())

    cli.run_pipeline(
        dataset_path="dataset.csv",
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        config_path=None,
        model_output=None,
        test_set_output="test_set.csv",
        feature_columns_output="feature_columns.json",
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
    )

    # C2로 feast-only: build-features에 output_path + 기간만 넘긴다(duckdb 인자 없음).
    assert build_features_call == {
        "output_path": "dataset.csv",
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
    }


def test_run_pipeline_logs_feast_lineage_as_train_extra_params(monkeypatch):
    from src.features.feast_retrieval import DEFAULT_SERVICE

    train_call = {}
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())
    monkeypatch.setattr(cli.train, "main", lambda **kw: train_call.update(kw))
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())

    cli.run_pipeline(
        dataset_path=None,
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        config_path=None,
        model_output=None,
        test_set_output=None,
        feature_columns_output=None,
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
    )

    # feast-only lineage: assembly_source=feast + FeatureService + registry + 기간.
    assert train_call["extra_params"] == {
        "assembly_source": "feast",
        "feature_service": DEFAULT_SERVICE,
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
        "feast_registry_path": "gs://fake/registry.db",
    }


def test_promote_model_prints_ok_and_exits_zero_on_success(monkeypatch, capsys):
    monkeypatch.setattr(cli.promote, "main", lambda **kwargs: "4")

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
    )

    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "v4" in out


def test_promote_model_accepts_deprecated_calibration_flag_and_ignores_it(monkeypatch, capsys):
    # #390: calibration_model_name은 무시되지만, Airflow DAG 하위호환을 위해 인자는 받아들여야
    # 하고 promote.main으로는 전달되지 않아야 한다.
    captured = {}

    def _fake_main(**kwargs):
        captured.update(kwargs)
        return "4"

    monkeypatch.setattr(cli.promote, "main", _fake_main)

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
        calibration_model_name="ctr-calibration-model",
    )

    assert "calibration_model_name" not in captured
    assert "[OK]" in capsys.readouterr().out


def test_promote_model_prints_noop_message_when_no_candidate(monkeypatch, capsys):
    monkeypatch.setattr(cli.promote, "main", lambda **kwargs: None)

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
    )

    out = capsys.readouterr().out
    assert "no-op" in out


def test_promote_model_exits_nonzero_with_gate_rejected_prefix(monkeypatch, capsys):
    def _raise(**kwargs):
        raise cli.promote.GateRejectedError("게이트1 미달: 예시 사유")

    monkeypatch.setattr(cli.promote, "main", _raise)

    with pytest.raises(typer.Exit) as exc_info:
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
        )

    assert exc_info.value.exit_code == 1
    err = capsys.readouterr().err
    assert "[게이트 미달]" in err


def test_promote_model_exits_nonzero_with_error_prefix_on_unexpected_exception(
    monkeypatch, capsys
):
    def _raise(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(cli.promote, "main", _raise)

    with pytest.raises(typer.Exit) as exc_info:
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
        )

    assert exc_info.value.exit_code == 1
    err = capsys.readouterr().err
    assert "[에러]" in err
