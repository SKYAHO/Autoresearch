"""src/pipeline/build_feature_tables.py 단위 테스트.

WRITE_TRUNCATE/CREATE OR REPLACE TABLE 대신 TRUNCATE TABLE + INSERT INTO를
트랜잭션으로 묶는 패턴을 지킨다는 것과, 각 함수가 문서화된 SQL 구조를
그대로 생성한다는 것을 검증한다. 실제 BigQuery 호출은 fake client로 대체한다.
"""

from unittest.mock import MagicMock

import pytest

from src.pipeline import build_feature_tables


class _FakeQueryJob:
    def __init__(self, sql):
        self.sql = sql

    def result(self):
        return None


class _FakeClient:
    def __init__(self):
        self.queries = []

    def query(self, sql):
        self.queries.append(sql)
        return _FakeQueryJob(sql)


@pytest.mark.parametrize(
    "build_fn,table_name",
    [
        (build_feature_tables.build_user_static_feature, "user_static_feature"),
        (build_feature_tables.build_user_dynamic_feature, "user_dynamic_feature"),
        (build_feature_tables.build_video_feature, "video_feature"),
        (build_feature_tables.build_training_entity, "training_entity"),
    ],
)
def test_uses_truncate_insert_transaction_not_write_truncate(build_fn, table_name):
    client = _FakeClient()
    build_fn(client, project="proj", dataset="ds")

    assert len(client.queries) == 1
    sql = client.queries[0]

    # 스키마를 재추론하는 패턴은 절대 쓰지 않는다 (Terraform이 스키마를 소유).
    assert "CREATE OR REPLACE TABLE" not in sql
    assert "WRITE_TRUNCATE" not in sql

    # TRUNCATE + INSERT INTO를 트랜잭션으로 묶는다.
    assert "BEGIN TRANSACTION;" in sql
    assert f"TRUNCATE TABLE `proj.ds.{table_name}`;" in sql
    assert f"INSERT INTO `proj.ds.{table_name}`" in sql
    assert "COMMIT TRANSACTION;" in sql

    # TRUNCATE가 INSERT보다 먼저, COMMIT이 마지막이어야 한다 (순서 보장).
    truncate_pos = sql.index("TRUNCATE TABLE")
    insert_pos = sql.index("INSERT INTO")
    commit_pos = sql.index("COMMIT TRANSACTION")
    assert truncate_pos < insert_pos < commit_pos


def test_build_user_static_feature_sql_shape():
    client = _FakeClient()
    build_feature_tables.build_user_static_feature(client, project="proj", dataset="ds")
    sql = client.queries[0]
    assert "`proj.ds.asset_virtual_user_vu_1000`" in sql
    assert "preferred_topics" in sql
    assert "watch_time_band" in sql


def test_build_user_dynamic_feature_sql_shape():
    client = _FakeClient()
    build_feature_tables.build_user_dynamic_feature(client, project="proj", dataset="ds")
    sql = client.queries[0]
    assert "`proj.ds.data_lake_action_log`" in sql
    assert "`proj.ds.data_lake_youtube_trending_kr`" in sql
    assert "recent_view_count_7d" in sql
    assert "total_event_count_7d" in sql
    assert "INTERVAL 7 DAY" in sql
    assert "INTERVAL 30 DAY" in sql


def test_build_video_feature_sql_shape():
    client = _FakeClient()
    build_feature_tables.build_video_feature(client, project="proj", dataset="ds")
    sql = client.queries[0]
    assert "`proj.ds.data_lake_youtube_trending_kr`" in sql
    assert "channel_subscriber_count" in sql
    assert "channel_view_count" in sql
    assert "channel_video_count" in sql


def test_build_training_entity_sql_shape_and_params():
    client = _FakeClient()
    build_feature_tables.build_training_entity(
        client, project="proj", dataset="ds", dataset_id="test_ds_v1", label_window_sec=900
    )
    sql = client.queries[0]
    assert "`proj.ds.data_lake_action_log`" in sql
    assert "'test_ds_v1' AS dataset_id" in sql
    assert "INTERVAL 900 SECOND" in sql


def test_main_runs_all_four_tables_in_order(monkeypatch):
    fake_client = _FakeClient()
    fake_bigquery_module = MagicMock()
    fake_bigquery_module.Client.return_value = fake_client
    import sys

    monkeypatch.setitem(sys.modules, "google.cloud.bigquery", fake_bigquery_module)
    monkeypatch.setitem(sys.modules, "google.cloud", MagicMock(bigquery=fake_bigquery_module))

    build_feature_tables.main()

    assert len(fake_client.queries) == 4
    tables_in_order = [
        "user_static_feature",
        "user_dynamic_feature",
        "video_feature",
        "training_entity",
    ]
    for sql, table in zip(fake_client.queries, tables_in_order):
        assert f"TRUNCATE TABLE `{build_feature_tables.BIGQUERY_PROJECT}.{build_feature_tables.BIGQUERY_DATASET}.{table}`;" in sql
