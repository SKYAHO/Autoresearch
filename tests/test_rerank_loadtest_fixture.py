"""리랭킹 부하테스트 fixture의 결정성 및 BigQuery DML 안전성 계약을 검증한다."""

from datetime import UTC, datetime

import pytest

from autoresearch.loadtest.rerank_fixture import (
    FIXTURE_USER_ID,
    FIXTURE_VIDEO_IDS,
    build_fixture,
    targeted_delete_sql,
    targeted_insert_sql,
)


def test_fixture_has_exact_row_counts() -> None:
    """고정 fixture는 네 FeatureView에 필요한 정확한 행 수를 생성한다."""
    tables = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))
    rows = {table.name: table.rows for table in tables}

    assert FIXTURE_VIDEO_IDS[0] == "loadtest-video-001"
    assert FIXTURE_VIDEO_IDS[-1] == "loadtest-video-200"
    assert {name: len(value) for name, value in rows.items()} == {
        "user_static_feature": 1,
        "user_dynamic_feature": 1,
        "video_feature": 200,
        "user_category_similarity": 5,
    }


def test_fixture_rows_use_the_requested_timestamp_and_feature_contract() -> None:
    """생성값은 한 UTC 초 단위 시각과 FeatureView가 읽는 모든 원본 컬럼을 쓴다."""
    timestamp = datetime(2026, 8, 1, 12, 34, 56, tzinfo=UTC)
    tables = {table.name: table for table in build_fixture(timestamp)}

    assert tables["user_static_feature"].rows[0] == {
        "user_id": FIXTURE_USER_ID,
        "event_timestamp": timestamp,
        "age_group": "30s",
        "occupation": "engineer",
        "preferred_category": ["10", "20", "22"],
        "preferred_topics": ["technology", "engineering", "science"],
        "watch_time_band": "medium",
    }
    assert tables["user_dynamic_feature"].rows[0] == {
        "user_id": FIXTURE_USER_ID,
        "event_timestamp": timestamp,
        "recent_click_count_7d": 50,
        "recent_view_count_7d": 500,
        "recent_watch_time_7d": 36000,
        "recent_like_count_7d": 25,
        "historical_category_affinity": "10",
        "total_event_count_7d": 575,
    }
    assert tables["video_feature"].rows[0] == {
        "video_id": "loadtest-video-001",
        "event_timestamp": timestamp,
        "category_id": "10",
        "duration_sec": 61,
        "view_count": 100100,
        "like_ratio": 0.05,
        "comment_ratio": 0.005,
        "days_since_upload": 1,
        "channel_subscriber_count": 1000000,
        "channel_view_count": 50000000,
        "channel_video_count": 1000,
    }
    assert tables["user_category_similarity"].rows[-1]["topic_similarity"] == 0.50


def test_video_dml_is_exact_and_non_destructive() -> None:
    """영상 fixture DML은 200개 고정 video ID에만 한정된다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[2]
    delete_sql, config = targeted_delete_sql("project-1", "feast_offline_store", table)
    insert_sql = targeted_insert_sql("project-1", "feast_offline_store", table)

    assert "video_id IN UNNEST(@video_ids)" in delete_sql
    assert config.query_parameters[0].name == "video_ids"
    assert "loadtest-video-001" in insert_sql
    assert "WRITE_TRUNCATE" not in delete_sql + insert_sql
    assert "CREATE OR REPLACE" not in delete_sql + insert_sql


@pytest.mark.parametrize("index", [0, 1, 3])
def test_user_keyed_dml_deletes_only_the_fixed_user(index: int) -> None:
    """사용자 entity 테이블은 고정 loadtest user parameter로만 삭제한다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[index]
    delete_sql, config = targeted_delete_sql("project-1", "feast_offline_store", table)

    assert "user_id = @user_id" in delete_sql
    assert config.query_parameters[0].name == "user_id"
    assert config.query_parameters[0].value == FIXTURE_USER_ID


@pytest.mark.parametrize(
    ("project", "dataset"),
    [
        ("project; DROP SCHEMA x", "feast_offline_store"),
        ("project-1", "feast_offline_store; DROP TABLE x"),
        ("", "feast_offline_store"),
        ("project-1", "invalid-dataset"),
    ],
)
def test_dml_rejects_invalid_identifier(project: str, dataset: str) -> None:
    """BigQuery 식별자에 임의 SQL 단편을 허용하지 않는다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[0]

    with pytest.raises(ValueError):
        targeted_delete_sql(project, dataset, table)
    with pytest.raises(ValueError):
        targeted_insert_sql(project, dataset, table)
