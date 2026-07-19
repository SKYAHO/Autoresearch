import sys
from pathlib import Path
from unittest.mock import MagicMock

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.pipeline import build_training_dataset  # noqa: E402


def test_load_personas_reads_csv(tmp_path):
    csv_path = tmp_path / "personas.csv"
    pd.DataFrame({"uuid": ["u1"], "age": [25], "occupation": ["Student"]}).to_csv(
        csv_path, index=False
    )

    result = build_training_dataset.load_personas(str(csv_path))

    assert list(result["uuid"]) == ["u1"]


def test_load_personas_reads_parquet(tmp_path):
    parquet_path = tmp_path / "personas.parquet"
    pd.DataFrame({"uuid": ["u1"], "age": [25], "occupation": ["Student"]}).to_parquet(
        parquet_path
    )

    result = build_training_dataset.load_personas(str(parquet_path))

    assert list(result["uuid"]) == ["u1"]


def test_main_rejects_invalid_videos_source():
    with pytest.raises(ValueError, match="videos_source"):
        build_training_dataset.main(videos_source="not-a-real-source")


def test_load_videos_from_bigquery_queries_configured_table(monkeypatch):
    fake_df = pd.DataFrame({"video_id": ["v1"]})
    fake_query_job = MagicMock()
    fake_query_job.to_dataframe.return_value = fake_df
    fake_client = MagicMock()
    fake_client.query.return_value = fake_query_job

    fake_bigquery_module = MagicMock()
    fake_bigquery_module.Client.return_value = fake_client
    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery_module)
    monkeypatch.setitem(sys.modules, "google.cloud", MagicMock(bigquery=fake_bigquery_module))

    result = build_training_dataset.load_videos_from_bigquery()

    assert result is fake_df
    fake_client.query.assert_called_once()
    query_text = fake_client.query.call_args[0][0]
    assert build_training_dataset.BIGQUERY_VIDEOS_TABLE in query_text
    assert "video_category AS categoryId" in query_text
