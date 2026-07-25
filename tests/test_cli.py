from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest
import typer

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src import cli  # noqa: E402


def test_run_pipeline_forwards_bigquery_sources_to_build_features(monkeypatch):
    build_features_call = {}
    train_call = {}

    def fake_build_features(**kwargs):
        build_features_call.update(kwargs)

    def fake_train(**kwargs):
        train_call.update(kwargs)

    monkeypatch.setattr(cli.build_training_dataset, "main", fake_build_features)
    monkeypatch.setattr(cli.train, "main", fake_train)
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())

    cli.run_pipeline(
        raw_dir="raw",
        events_path=None,
        dataset_path="dataset.csv",
        videos_source="bigquery",
        personas_path="personas.csv",
        events_source="bigquery",
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        topic_similarity_source="inmemory",
        config_path=None,
        model_output=None,
        test_set_output="test_set.csv",
        feature_columns_output="feature_columns.pkl",
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
    )

    assert build_features_call["videos_source"] == "bigquery"
    assert build_features_call["events_source"] == "bigquery"
    assert build_features_call["events_start_date"] == "2026-07-01"
    assert build_features_call["events_end_date"] == "2026-07-08"
    assert build_features_call["personas_path"] == "personas.csv"


def test_run_pipeline_logs_data_source_lineage_as_train_extra_params(monkeypatch):
    train_call = {}

    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())
    monkeypatch.setattr(cli.train, "main", lambda **kwargs: train_call.update(kwargs))
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())

    cli.run_pipeline(
        raw_dir=None,
        events_path=None,
        dataset_path=None,
        videos_source="bigquery",
        personas_path=None,
        events_source="bigquery",
        events_start_date="2026-07-01",
        events_end_date="2026-07-08",
        topic_similarity_source="bigquery",
        config_path=None,
        model_output=None,
        test_set_output=None,
        feature_columns_output=None,
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
    )

    assert train_call["extra_params"] == {
        "videos_source": "bigquery",
        "events_source": "bigquery",
        "topic_similarity_source": "bigquery",
        "events_start_date": "2026-07-01",
        "events_end_date": "2026-07-08",
    }


def test_run_pipeline_omits_event_dates_from_extra_params_for_csv_source(monkeypatch):
    train_call = {}

    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())
    monkeypatch.setattr(cli.train, "main", lambda **kwargs: train_call.update(kwargs))
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())

    cli.run_pipeline(
        raw_dir=None,
        events_path=None,
        dataset_path=None,
        videos_source="csv",
        personas_path=None,
        events_source="csv",
        events_start_date=None,
        events_end_date=None,
        topic_similarity_source="inmemory",
        config_path=None,
        model_output=None,
        test_set_output=None,
        feature_columns_output=None,
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
    )

    assert train_call["extra_params"] == {
        "videos_source": "csv",
        "events_source": "csv",
        "topic_similarity_source": "inmemory",
    }


def test_promote_model_prints_ok_and_exits_zero_on_success(monkeypatch, capsys):
    monkeypatch.setattr(cli.promote, "main", lambda **kwargs: "4")

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
        calibration_model_name="ctr-calibration-model",
    )

    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "v4" in out


def test_promote_model_prints_noop_message_when_no_candidate(monkeypatch, capsys):
    monkeypatch.setattr(cli.promote, "main", lambda **kwargs: None)

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
        calibration_model_name="ctr-calibration-model",
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
            calibration_model_name="ctr-calibration-model",
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
            calibration_model_name="ctr-calibration-model",
        )

    assert exc_info.value.exit_code == 1
    err = capsys.readouterr().err
    assert "[에러]" in err
