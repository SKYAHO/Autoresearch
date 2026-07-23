"""autoresearch.jobs.feature_store_build 공개 batch 계약 테스트."""

from __future__ import annotations

import json
from datetime import date

import pytest

from autoresearch.jobs import feature_store_build

_PARTITION_DATE = date(2026, 7, 21)
_PARTITION_ARGS = ["--partition-date", "2026-07-21"]


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


def _incremental_sql(spec, **overrides) -> str:
    kwargs = {
        "project": "p",
        "dataset": "feat",
        "raw_dataset": "raw",
        "partition_date": _PARTITION_DATE,
    }
    kwargs.update(overrides)
    return feature_store_build.build_incremental_sql(spec, **kwargs)


def test_default_tables_cover_declared_specs() -> None:
    assert [spec.name for spec in feature_store_build.FEATURE_TABLES] == [
        "user_dynamic_feature",
        "video_feature",
    ]


def test_static_feature_tables_are_not_owned_by_this_command() -> None:
    # user_static_feature / user_category_similarity는 날짜 개념이 없는 정적
    # feature라 scripts/build_static_features.py가 소유한다.
    assert not hasattr(feature_store_build, "USER_STATIC_FEATURE")
    assert "asset_virtual_user_vu_1000" not in _incremental_sql(
        feature_store_build.USER_DYNAMIC_FEATURE
    )


def test_incremental_sql_deletes_target_date_then_inserts() -> None:
    sql = _incremental_sql(
        feature_store_build.VIDEO_FEATURE, dataset="feast_offline_store"
    )
    assert sql.startswith(
        "DELETE FROM `p.feast_offline_store.video_feature`\n"
        "WHERE DATE(event_timestamp, 'Asia/Seoul') = DATE '2026-07-21';"
    )
    assert "INSERT INTO `p.feast_offline_store.video_feature` (" in sql
    # 전체를 걷어내는 구문과 Terraform 소유 스키마를 덮어쓰는 구문은 나오면 안 된다.
    assert "TRUNCATE TABLE" not in sql
    assert "CREATE OR REPLACE" not in sql
    assert "WRITE_TRUNCATE" not in sql


def test_video_incremental_sql_reads_only_the_target_date_from_raw() -> None:
    sql = _incremental_sql(feature_store_build.VIDEO_FEATURE)
    assert "DATE(collected_at, 'Asia/Seoul') = DATE '2026-07-21'" in sql


def test_user_dynamic_incremental_sql_builds_a_single_snapshot() -> None:
    sql = _incremental_sql(feature_store_build.USER_DYNAMIC_FEATURE)
    assert sql.startswith(
        "DELETE FROM `p.feat.user_dynamic_feature`\n"
        "WHERE event_timestamp = TIMESTAMP(DATE '2026-07-21', 'Asia/Seoul');"
    )
    # 전 기간 스냅샷 grid를 만들던 구문이 사라져야 한다.
    assert "GENERATE_DATE_ARRAY" not in sql
    # raw 스캔은 30일 룩백 윈도우로 제한된다.
    assert sql.count("INTERVAL 30 DAY") >= 2
    assert "AND event_timestamp < TIMESTAMP(DATE '2026-07-21', 'Asia/Seoul')" in sql


def test_user_dynamic_snapshot_prunes_action_log_partitions_with_between() -> None:
    # A안(#295): dt=D 파티션은 KST D일 하루치 슬라이스다. 30일 히스토리는
    # dt BETWEEN P-30 AND P-1 프루닝 + timestamp 윈도우로 조립한다.
    sql = _incremental_sql(feature_store_build.USER_DYNAMIC_FEATURE)

    assert "AND dt = DATE '2026-07-21'" not in sql
    assert (
        "AND dt BETWEEN DATE_SUB(DATE '2026-07-21', INTERVAL 30 DAY)" in sql
    )
    assert "AND DATE_SUB(DATE '2026-07-21', INTERVAL 1 DAY)" in sql


def test_user_dynamic_snapshot_covers_users_already_in_the_feature_table() -> None:
    # 룩백 윈도우에 활동이 없는 유저도 행을 받아야 Feast가 stale한 과거 스냅샷으로
    # fallback하지 않는다.
    sql = _incremental_sql(feature_store_build.USER_DYNAMIC_FEATURE)
    assert "FROM `p.feat.user_dynamic_feature`" in sql
    assert "UNION DISTINCT" in sql


def test_incremental_sql_resolves_raw_and_feature_datasets_separately() -> None:
    sql = _incremental_sql(feature_store_build.USER_DYNAMIC_FEATURE)
    assert "`p.raw.data_lake_action_log`" in sql
    assert "`p.raw.data_lake_youtube_trending_kr`" in sql
    assert "INSERT INTO `p.feat.user_dynamic_feature`" in sql


def test_insert_column_list_matches_feature_view_contract() -> None:
    assert feature_store_build.USER_DYNAMIC_FEATURE.columns[:2] == (
        "user_id",
        "event_timestamp",
    )
    assert feature_store_build.VIDEO_FEATURE.columns[:3] == (
        "video_id",
        "event_timestamp",
        "category_id",
    )


def test_validation_sql_checks_empty_null_and_duplicate_keys() -> None:
    sql = feature_store_build.build_validation_sql(
        feature_store_build.VIDEO_FEATURE,
        project="p",
        dataset="feat",
        partition_date=_PARTITION_DATE,
    )
    assert "STRUCT(video_id, event_timestamp)" in sql
    assert "video_id IS NULL OR event_timestamp IS NULL" in sql
    assert sql.count("ERROR(") == 3


def test_validation_sql_is_scoped_to_the_target_date() -> None:
    sql = feature_store_build.build_validation_sql(
        feature_store_build.USER_DYNAMIC_FEATURE,
        project="p",
        dataset="feat",
        partition_date=_PARTITION_DATE,
    )
    assert (
        "WHERE event_timestamp = TIMESTAMP(DATE '2026-07-21', 'Asia/Seoul')" in sql
    )


def test_main_loads_and_validates_every_table(fake_client, capsys) -> None:
    exit_code = feature_store_build.main(
        ["--project", "p", "--dataset", "feat", "--raw-dataset", "raw"]
        + _PARTITION_ARGS
    )

    assert exit_code == 0
    assert len(fake_client.queries) == 2 * len(feature_store_build.FEATURE_TABLES)
    assert all(not dry_run for _, dry_run, _ in fake_client.queries)
    summary = _summary_line(capsys)
    assert summary["job"] == "feature_store_build"
    assert summary["status"] == "succeeded"
    assert summary["mode"] == "incremental"
    assert summary["partition_date"] == "2026-07-21"
    assert summary["tables"] == ["user_dynamic_feature", "video_feature"]


def test_main_deletes_before_inserting_for_each_table(fake_client) -> None:
    feature_store_build.main(["--tables", "video_feature"] + _PARTITION_ARGS)

    load_sql, _ = (sql for sql, _, _ in fake_client.queries)
    assert load_sql.index("DELETE FROM") < load_sql.index("INSERT INTO")


def test_main_table_subset_runs_only_requested_table(fake_client, capsys) -> None:
    exit_code = feature_store_build.main(
        ["--tables", "video_feature"] + _PARTITION_ARGS
    )

    assert exit_code == 0
    assert len(fake_client.queries) == 2
    summary = _summary_line(capsys)
    assert summary["tables"] == ["video_feature"]


def test_main_dry_run_does_not_write(fake_client, capsys) -> None:
    exit_code = feature_store_build.main(["--dry-run"] + _PARTITION_ARGS)

    assert exit_code == 0
    assert all(dry_run for _, dry_run, _ in fake_client.queries)
    assert _summary_line(capsys)["mode"] == "dry_run"


def test_main_rejects_unknown_table(capsys) -> None:
    exit_code = feature_store_build.main(
        ["--tables", "user_category_similarity"] + _PARTITION_ARGS
    )

    assert exit_code == 2
    summary = _summary_line(capsys)
    assert summary["status"] == "failed"
    assert summary["error_type"] == "invalid_arguments"


def test_main_rejects_static_feature_table(capsys) -> None:
    exit_code = feature_store_build.main(
        ["--tables", "user_static_feature"] + _PARTITION_ARGS
    )

    assert exit_code == 2
    assert _summary_line(capsys)["error_type"] == "invalid_arguments"


def test_main_requires_partition_date(capsys) -> None:
    exit_code = feature_store_build.main(["--tables", "video_feature"])

    assert exit_code == 2
    assert _summary_line(capsys)["error_type"] == "invalid_arguments"


@pytest.mark.parametrize("value", ["2026-13-01", "20260721", "yesterday", ""])
def test_main_rejects_malformed_partition_date(capsys, value: str) -> None:
    exit_code = feature_store_build.main(["--partition-date", value])

    assert exit_code == 2
    assert _summary_line(capsys)["error_type"] == "invalid_arguments"


def test_main_rejects_identical_raw_and_feature_dataset(capsys) -> None:
    exit_code = feature_store_build.main(
        ["--dataset", "same", "--raw-dataset", "same"] + _PARTITION_ARGS
    )

    assert exit_code == 2
    assert _summary_line(capsys)["error_type"] == "invalid_arguments"


def test_main_maps_runtime_failure_to_exit_one(monkeypatch, capsys) -> None:
    def _boom(project: str, location: str):
        raise RuntimeError("bigquery unavailable")

    monkeypatch.setattr(feature_store_build, "_client", _boom)

    exit_code = feature_store_build.main(_PARTITION_ARGS)

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
