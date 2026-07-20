from __future__ import annotations

import sys
from pathlib import Path
from unittest.mock import MagicMock

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
        config_path=None,
        model_output=None,
        test_set_output=None,
        feature_columns_output=None,
        categorical_columns_output=None,
        test_size=None,
        val_size=None,
        random_state=None,
    )

    assert train_call["extra_params"] == {"videos_source": "csv", "events_source": "csv"}
