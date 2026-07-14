"""tests for scripts/load_raw_to_bigquery.py (BigQuery 클라이언트 mock, 실제 GCP 호출 없음)."""

from unittest import mock

import pytest
from google.cloud import bigquery

from scripts.load_raw_to_bigquery import (
    LOAD_TARGETS,
    build_job_config,
    build_source_uri,
    load_target,
    main,
    select_targets,
)

BUCKET = "test-lake-bucket"


def _target(key: str):
    return next(t for t in LOAD_TARGETS if t.key == key)


# ---------------------------------------------------------------------------
# build_source_uri
# ---------------------------------------------------------------------------

def test_build_source_uri_hive_partitioned():
    uri = build_source_uri(BUCKET, _target("action_log"))
    assert uri == f"gs://{BUCKET}/data_lake/action_log/*"


def test_build_source_uri_single_file():
    uri = build_source_uri(BUCKET, _target("virtual_user"))
    assert uri == f"gs://{BUCKET}/asset/virtual_user/vu_1000.parquet"


# ---------------------------------------------------------------------------
# build_job_config
# ---------------------------------------------------------------------------

def test_build_job_config_parquet_truncate():
    config = build_job_config(BUCKET, _target("virtual_user"))
    assert config.source_format == bigquery.SourceFormat.PARQUET
    assert config.write_disposition == bigquery.WriteDisposition.WRITE_TRUNCATE


def test_build_job_config_hive_partitioning():
    config = build_job_config(BUCKET, _target("youtube_trending_kr"))
    assert config.hive_partitioning is not None
    assert config.hive_partitioning.mode == "AUTO"
    assert (
        config.hive_partitioning.source_uri_prefix
        == f"gs://{BUCKET}/data_lake/youtube_trending_kr"
    )


def test_build_job_config_single_file_has_no_hive_partitioning():
    config = build_job_config(BUCKET, _target("virtual_user"))
    assert config.hive_partitioning is None


# ---------------------------------------------------------------------------
# select_targets
# ---------------------------------------------------------------------------

def test_select_targets_default_returns_all():
    assert select_targets(None) == LOAD_TARGETS


def test_select_targets_subset_preserves_request_order():
    targets = select_targets("virtual_user,action_log")
    assert [t.key for t in targets] == ["virtual_user", "action_log"]


def test_select_targets_unknown_key_raises():
    with pytest.raises(ValueError, match="unknown_table"):
        select_targets("action_log,unknown_table")


# ---------------------------------------------------------------------------
# load_target
# ---------------------------------------------------------------------------

def test_load_target_success_returns_row_count():
    client = mock.MagicMock()
    client.get_table.return_value.num_rows = 1234
    target = _target("action_log")

    result = load_target(
        client=client,
        project="proj",
        dataset="ds",
        location="asia-northeast3",
        bucket=BUCKET,
        target=target,
    )

    assert result.ok
    assert result.num_rows == 1234
    args, kwargs = client.load_table_from_uri.call_args
    assert args[0] == f"gs://{BUCKET}/data_lake/action_log/*"
    assert args[1] == "proj.ds.data_lake_action_log"
    assert kwargs["location"] == "asia-northeast3"
    client.load_table_from_uri.return_value.result.assert_called_once()


def test_load_target_failure_is_captured():
    client = mock.MagicMock()
    client.load_table_from_uri.return_value.result.side_effect = RuntimeError("boom")

    result = load_target(
        client=client,
        project="proj",
        dataset="ds",
        location="asia-northeast3",
        bucket=BUCKET,
        target=_target("virtual_user"),
    )

    assert not result.ok
    assert result.num_rows is None
    assert "boom" in result.error


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

@pytest.fixture
def isolated_env(monkeypatch):
    monkeypatch.setattr("scripts.load_raw_to_bigquery.load_dotenv", lambda: None)
    for var in ("GCP_PROJECT_ID", "BQ_DATASET", "BQ_LOCATION", "YOUTUBE_LAKE_BUCKET"):
        monkeypatch.delenv(var, raising=False)


def test_main_missing_bucket_exits_with_error(isolated_env, capsys):
    with mock.patch("scripts.load_raw_to_bigquery.bigquery.Client") as client_cls:
        exit_code = main(["--project", "proj"])

    assert exit_code == 1
    assert "YOUTUBE_LAKE_BUCKET" in capsys.readouterr().out
    client_cls.assert_not_called()


def test_main_missing_project_exits_with_error(isolated_env, capsys):
    with mock.patch("scripts.load_raw_to_bigquery.bigquery.Client") as client_cls:
        exit_code = main(["--bucket", BUCKET])

    assert exit_code == 1
    assert "GCP_PROJECT_ID" in capsys.readouterr().out
    client_cls.assert_not_called()


def test_main_one_failure_does_not_block_others(isolated_env, capsys):
    client = mock.MagicMock()
    client.get_table.return_value.num_rows = 10

    def fake_load(uri, table_id, location, job_config):
        job = mock.MagicMock()
        if "action_log" in table_id:
            job.result.side_effect = RuntimeError("load failed")
        return job

    client.load_table_from_uri.side_effect = fake_load

    with mock.patch(
        "scripts.load_raw_to_bigquery.bigquery.Client", return_value=client
    ):
        exit_code = main(["--project", "proj", "--bucket", BUCKET])

    assert exit_code == 1
    assert client.load_table_from_uri.call_count == len(LOAD_TARGETS)
    out = capsys.readouterr().out
    assert "data_lake_action_log" in out
    assert "[FAIL]" in out


def test_main_all_success_returns_zero(isolated_env):
    client = mock.MagicMock()
    client.get_table.return_value.num_rows = 10

    with mock.patch(
        "scripts.load_raw_to_bigquery.bigquery.Client", return_value=client
    ):
        exit_code = main(["--project", "proj", "--bucket", BUCKET])

    assert exit_code == 0
    assert client.load_table_from_uri.call_count == len(LOAD_TARGETS)


def test_main_unknown_table_key_exits_with_error(isolated_env, capsys):
    with mock.patch("scripts.load_raw_to_bigquery.bigquery.Client") as client_cls:
        exit_code = main(
            ["--project", "proj", "--bucket", BUCKET, "--tables", "nope"]
        )

    assert exit_code == 1
    assert "nope" in capsys.readouterr().out
    client_cls.assert_not_called()
