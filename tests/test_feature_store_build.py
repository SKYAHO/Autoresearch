"""autoresearch.jobs.feature_store_build 공개 batch 계약 테스트."""

from __future__ import annotations

import json

import pytest

from autoresearch.jobs import feature_store_build


class _FakeQueryJob:
    def __init__(self) -> None:
        self.result_calls = 0

    def result(self) -> None:
        self.result_calls += 1


class _FakeClient:
    def __init__(self) -> None:
        self.queries: list[tuple[str, bool, str]] = []

    def query(self, sql, *, job_config, location):
        self.queries.append((sql, bool(job_config.dry_run), location))
        return _FakeQueryJob()


class _FakeJobConfig:
    def __init__(self, *, dry_run: bool = False, use_query_cache: bool = True) -> None:
        self.dry_run = dry_run
        self.use_query_cache = use_query_cache


@pytest.fixture
def fake_client(monkeypatch) -> _FakeClient:
    client = _FakeClient()
    monkeypatch.setattr(
        feature_store_build, "_client", lambda project, location: client
    )
    monkeypatch.setattr(
        feature_store_build,
        "_run_query",
        lambda c, sql, *, location, dry_run: c.query(
            sql,
            job_config=_FakeJobConfig(dry_run=dry_run),
            location=location,
        ),
    )
    return client


def _summary_line(capsys) -> dict:
    lines = [line for line in capsys.readouterr().out.splitlines() if line.strip()]
    return json.loads(lines[-1])


def test_default_tables_cover_declared_specs() -> None:
    assert [spec.name for spec in feature_store_build.FEATURE_TABLES] == [
        "user_static_feature",
        "user_dynamic_feature",
        "video_feature",
    ]


def test_rebuild_sql_truncates_then_inserts_without_replacing_schema() -> None:
    sql = feature_store_build.build_rebuild_sql(
        feature_store_build.VIDEO_FEATURE,
        project="p",
        dataset="feast_offline_store",
        raw_dataset="data_lake_raw",
    )
    assert sql.startswith("TRUNCATE TABLE `p.feast_offline_store.video_feature`;")
    assert "INSERT INTO `p.feast_offline_store.video_feature` (" in sql
    # Terraform이 소유한 스키마를 덮어쓰는 구문은 절대 나오면 안 된다.
    assert "CREATE OR REPLACE" not in sql
    assert "WRITE_TRUNCATE" not in sql


def test_rebuild_sql_resolves_raw_and_feature_datasets_separately() -> None:
    dynamic_sql = feature_store_build.build_rebuild_sql(
        feature_store_build.USER_DYNAMIC_FEATURE,
        project="p",
        dataset="feat",
        raw_dataset="raw",
    )
    assert "`p.raw.data_lake_action_log`" in dynamic_sql
    assert "`p.raw.data_lake_youtube_trending_kr`" in dynamic_sql
    assert "INSERT INTO `p.feat.user_dynamic_feature`" in dynamic_sql

    static_sql = feature_store_build.build_rebuild_sql(
        feature_store_build.USER_STATIC_FEATURE,
        project="p",
        dataset="feat",
        raw_dataset="raw",
    )
    assert "`p.feat.asset_virtual_user_vu_1000`" in static_sql


def test_insert_column_list_matches_feature_view_contract() -> None:
    assert feature_store_build.USER_STATIC_FEATURE.columns == (
        "user_id",
        "event_timestamp",
        "age_group",
        "occupation",
        "preferred_category",
        "preferred_topics",
        "watch_time_band",
    )
    assert feature_store_build.VIDEO_FEATURE.columns[:3] == (
        "video_id",
        "event_timestamp",
        "category_id",
    )


def test_validation_sql_checks_empty_null_and_duplicate_keys() -> None:
    sql = feature_store_build.build_validation_sql(
        feature_store_build.VIDEO_FEATURE, project="p", dataset="feat"
    )
    assert "STRUCT(video_id, event_timestamp)" in sql
    assert "video_id IS NULL OR event_timestamp IS NULL" in sql
    assert sql.count("ERROR(") == 3


def test_main_rebuilds_and_validates_every_table(fake_client, capsys) -> None:
    exit_code = feature_store_build.main(
        ["--project", "p", "--dataset", "feat", "--raw-dataset", "raw"]
    )

    assert exit_code == 0
    assert len(fake_client.queries) == 2 * len(feature_store_build.FEATURE_TABLES)
    assert all(not dry_run for _, dry_run, _ in fake_client.queries)
    summary = _summary_line(capsys)
    assert summary["job"] == "feature_store_build"
    assert summary["status"] == "succeeded"
    assert summary["mode"] == "rebuild"
    assert summary["tables"] == [
        "user_static_feature",
        "user_dynamic_feature",
        "video_feature",
    ]


def test_main_table_subset_runs_only_requested_table(fake_client, capsys) -> None:
    exit_code = feature_store_build.main(["--tables", "video_feature"])

    assert exit_code == 0
    assert len(fake_client.queries) == 2
    summary = _summary_line(capsys)
    assert summary["tables"] == ["video_feature"]


def test_main_dry_run_does_not_write(fake_client, capsys) -> None:
    exit_code = feature_store_build.main(["--dry-run"])

    assert exit_code == 0
    assert all(dry_run for _, dry_run, _ in fake_client.queries)
    assert _summary_line(capsys)["mode"] == "dry_run"


def test_main_rejects_unknown_table(capsys) -> None:
    exit_code = feature_store_build.main(["--tables", "user_category_similarity"])

    assert exit_code == 2
    summary = _summary_line(capsys)
    assert summary["status"] == "failed"
    assert summary["error_type"] == "invalid_arguments"


def test_main_rejects_identical_raw_and_feature_dataset(capsys) -> None:
    exit_code = feature_store_build.main(
        ["--dataset", "same", "--raw-dataset", "same"]
    )

    assert exit_code == 2
    assert _summary_line(capsys)["error_type"] == "invalid_arguments"


def test_main_maps_runtime_failure_to_exit_one(monkeypatch, capsys) -> None:
    def _boom(project: str, location: str):
        raise RuntimeError("bigquery unavailable")

    monkeypatch.setattr(feature_store_build, "_client", _boom)

    exit_code = feature_store_build.main([])

    assert exit_code == 1
    summary = _summary_line(capsys)
    assert summary["status"] == "failed"
    assert summary["error_type"] == "runtime_failure"


def test_version_reports_batch_contract(capsys) -> None:
    with pytest.raises(SystemExit) as excinfo:
        feature_store_build.main(["--version"])

    assert excinfo.value.code == 0
    payload = json.loads(capsys.readouterr().out.strip())
    assert payload["contract_version"] == "batch-contract-v1"
