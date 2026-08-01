"""autoresearch.jobs.feature_store_build 공개 batch 계약 테스트."""

from __future__ import annotations

import json
from datetime import date

import pytest

from autoresearch.jobs import feature_store_build

_PARTITION_DATE = date(2026, 7, 21)
_PARTITION_ARGS = ["--partition-date", "2026-07-21"]


@pytest.fixture(autouse=True)
def configured_project(monkeypatch) -> None:
    """명시 프로젝트가 필요한 정상 경로에 controlled input을 제공한다."""
    monkeypatch.setenv("CTR_TRAINING_BQ_PROJECT", "test-project")


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
        "training_entity",
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


def test_training_entity_columns_match_spine_contract() -> None:
    # data-warehouse.md training_entity 절과 동일한 컬럼·순서. entity_keys는 Feast
    # join key(user, video)이고 event_timestamp가 PIT 조회 기준 시점이다.
    assert feature_store_build.TRAINING_ENTITY.columns == (
        "dataset_id",
        "user_id",
        "video_id",
        "event_timestamp",
        "clicked",
        "source_event_id",
    )
    assert feature_store_build.TRAINING_ENTITY.entity_keys == ("user_id", "video_id")


def test_training_entity_output_and_delete_scope_is_day_d_only() -> None:
    # 이 파티션이 소유(삭제·검증·출력)하는 범위는 impression이 KST 날짜 D에 발생한
    # 행뿐이다. click 스캔·귀속 후보가 D+1까지 넓어도 출력은 D로 잘린다.
    sql = _incremental_sql(feature_store_build.TRAINING_ENTITY)
    assert sql.startswith(
        "DELETE FROM `p.feat.training_entity`\n"
        "WHERE DATE(event_timestamp, 'Asia/Seoul') = DATE '2026-07-21';"
    )
    assert "INSERT INTO `p.feat.training_entity` (" in sql
    # 출력 CTE는 D 자정 상한으로 잘린다(= partition_predicate와 같은 범위).
    assert "impressions_output AS (" in sql
    assert (
        "WHERE event_timestamp < TIMESTAMP(\n"
        "    DATE_ADD(DATE '2026-07-21', INTERVAL 1 DAY), 'Asia/Seoul')" in sql
    )
    assert "TRUNCATE TABLE" not in sql
    assert "CREATE OR REPLACE" not in sql


def test_training_entity_clicks_scan_extends_into_next_day_and_30min() -> None:
    # 자정을 넘긴 click 귀속을 잡으려면 click 스캔이 D+1 파티션 + 30분까지 가야 한다.
    sql = _incremental_sql(feature_store_build.TRAINING_ENTITY)
    # impression 후보 스캔과 click 스캔 두 곳 모두 dt를 D+1까지 넓힌다.
    assert sql.count("dt BETWEEN DATE '2026-07-21'") == 2
    assert (
        "AND DATE_ADD(DATE '2026-07-21', INTERVAL 1 DAY)" in sql
    )
    # click 상한은 D+1 자정 + 30분(1800초). 귀속 창(TIMESTAMP_SUB)과 합쳐 두 번 등장.
    assert "TIMESTAMP_ADD(" in sql
    assert sql.count("INTERVAL 1800 SECOND") == 2


def test_training_entity_attribution_candidate_pool_spans_next_day_against_cross_midnight_double_positive() -> None:
    """자정 경계 이중 positive 회귀 가드 (#245, spec 3-way 설계).

    시나리오: 같은 (user, video)가 D=2026-07-21 23:55와 D+1=2026-07-22 00:05에
    각각 노출되고, click이 D+1 00:10에 온다. 진짜 귀속 대상은 더 최근인 D+1 00:05
    impression이다. 귀속 후보를 D로만 좁히면 이 click이 D 23:55 impression에 잘못
    붙어 D 행이 거짓 clicked=1이 되고(+ D+1 빌드가 같은 click을 정상 귀속시켜 물리적
    click 하나가 두 행에서 positive가 됨), 이 버그는 유일성 위반이 아니라 semantic
    오류라 build_validation_sql의 null/dup-key 검사로는 잡히지 않는다.

    이 하네스는 SQL 문자열 검증(실행 아님)이라, 버그를 유발하는 유일한 회귀 —
    "귀속 후보를 출력과 같은 D 단일 날짜로 좁히는 것" — 을 구조로 고정한다: 귀속
    후보(impressions_candidates)는 dt D∪D+1을 스캔하고, 전역 최근 1건을 고르며,
    출력만 D로 제한된다. 실제 값 대조(D 행이 clicked=0)는 #299 Phase 0의 offline
    조회 스모크(실행 하네스)에서 같은 시나리오로 재확인한다.
    """
    sql = _incremental_sql(feature_store_build.TRAINING_ENTITY)
    # 후보 impression 풀은 출력(D)과 분리된 별도 CTE이며 D+1까지 스캔한다.
    assert "impressions_candidates AS (" in sql
    assert "impressions_output AS (" in sql
    candidate_block = sql.split("impressions_output AS (")[0]
    assert (
        "dt BETWEEN DATE '2026-07-21'\n"
        "               AND DATE_ADD(DATE '2026-07-21', INTERVAL 1 DAY)"
        in candidate_block
    )
    # click은 후보 풀 전체에서 "가장 최근" impression 1건에 귀속된다.
    assert "PARTITION BY c.click_event_id" in sql
    assert "ORDER BY i.event_timestamp DESC" in sql
    assert "WHERE rn = 1" in sql
    # 출력은 여전히 D 자정 상한으로 잘린다(D+1 impression은 D+1 빌드가 소유).
    assert feature_store_build.TRAINING_ENTITY.partition_predicate == (
        "DATE(event_timestamp, 'Asia/Seoul') = DATE '{partition_date}'"
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
    assert summary["tables"] == [
        "user_dynamic_feature",
        "video_feature",
        "training_entity",
    ]


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


def test_main_requires_explicit_project_before_creating_client(
    monkeypatch, caplog, capsys
) -> None:
    """기본 프로젝트가 없으면 BigQuery 클라이언트를 만들기 전에 중단한다."""
    monkeypatch.delenv("CTR_TRAINING_BQ_PROJECT", raising=False)

    def _should_not_be_called(project: str, location: str) -> None:
        raise AssertionError("프로젝트 없이 BigQuery 클라이언트를 만들면 안 됩니다")

    monkeypatch.setattr(feature_store_build, "_client", _should_not_be_called)

    exit_code = feature_store_build.main(_PARTITION_ARGS)

    assert exit_code == 2
    assert "CTR_TRAINING_BQ_PROJECT" in caplog.text
    assert "--project" in caplog.text
    summary = _summary_line(capsys)
    assert summary["status"] == "failed"
    assert summary["error_type"] == "invalid_arguments"


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
