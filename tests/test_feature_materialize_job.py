import pytest

import autoresearch.jobs.feature_materialize as feature_materialize


def test_feature_tables_are_the_three_supported_sources():
    assert feature_materialize.FEATURE_TABLES == (
        "user_static_feature",
        "user_dynamic_feature",
        "video_feature",
    )


def test_static_script_flattens_bigquery_parquet_list_wrappers():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "user_static_feature"
    )

    assert "UNNEST(primary_categories.list) AS item" in script
    assert "item.element" in script
    assert "ARRAY<STRING>[]" in script
    assert "asset_virtual_user_vu_1000" in script


@pytest.mark.parametrize(
    "table_name,raw_table",
    [
        ("user_dynamic_feature", "data_lake_action_log"),
        ("video_feature", "data_lake_youtube_trending_kr"),
    ],
)
def test_supported_script_references_its_raw_source(table_name, raw_table):
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", table_name
    )

    assert raw_table in script
    assert "BEGIN TRANSACTION" in script
    assert "DELETE FROM" in script
    assert "INSERT INTO" in script
    assert "ASSERT" in script
    assert "CREATE OR REPLACE TABLE" not in script


def test_script_rejects_unknown_feature_table():
    with pytest.raises(ValueError, match="unsupported feature table"):
        feature_materialize.build_materialize_script(
            "test-project", "test_dataset", "user_category_similarity"
        )
