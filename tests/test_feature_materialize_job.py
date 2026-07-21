import re

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
    table_name = "user_category_similarity; secret-value"

    with pytest.raises(ValueError, match="^unsupported feature table$") as error:
        feature_materialize.build_materialize_script(
            "test-project", "test_dataset", table_name
        )

    assert table_name not in str(error.value)


@pytest.mark.parametrize(
    ("project_id", "dataset_id", "field_name"),
    [
        ("test project", "test_dataset", "project_id"),
        ("test`project", "test_dataset", "project_id"),
        ("test-project; DROP TABLE users", "test_dataset", "project_id"),
        ("test-project", "test dataset", "dataset_id"),
        ("test-project", "test`dataset", "dataset_id"),
        ("test-project", "test_dataset; DROP TABLE users", "dataset_id"),
    ],
)
def test_script_rejects_unsafe_project_or_dataset_identifier(
    project_id, dataset_id, field_name
):
    with pytest.raises(ValueError, match=field_name) as error:
        feature_materialize.build_materialize_script(
            project_id, dataset_id, "user_static_feature"
        )

    assert project_id not in str(error.value)
    assert dataset_id not in str(error.value)


@pytest.mark.parametrize("project_id", [None, 1, object()])
def test_script_rejects_non_string_project_identifier(project_id):
    with pytest.raises(ValueError, match="^invalid project_id$"):
        feature_materialize.build_materialize_script(
            project_id, "test_dataset", "user_static_feature"
        )


@pytest.mark.parametrize("dataset_id", [None, 1, object()])
def test_script_rejects_non_string_dataset_identifier(dataset_id):
    with pytest.raises(ValueError, match="^invalid dataset_id$"):
        feature_materialize.build_materialize_script(
            "test-project", dataset_id, "user_static_feature"
        )


def test_video_script_uses_single_backslash_iso_8601_duration_patterns():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "video_feature"
    )

    for pattern in (r"P(\d+)D", r"(\d+)H", r"(\d+)M", r"(\d+)S"):
        assert f"r'{pattern}'" in script


def test_video_script_uses_safe_duration_arithmetic():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "video_feature"
    )

    assert "SAFE_MULTIPLY(" in script
    assert "SAFE_ADD(" in script
    assert re.search(r"COALESCE\(\s*SAFE_MULTIPLY\(", script)


def test_video_script_duration_contract_composes_pt1d2h3m4s_as_93784_seconds():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "video_feature"
    )

    expected_duration_sec = 1 * 86400 + 2 * 3600 + 3 * 60 + 4

    assert expected_duration_sec == 93784
    assert "SAFE_MULTIPLY(" in script
    assert "86400" in script
    assert "3600" in script
    assert "60" in script
    assert "COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\\d+)S') AS INT64), 0)" in script


def test_video_script_duration_contract_propagates_safe_add_overflow_to_outer_coalesce():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "video_feature"
    )

    duration_expression = """COALESCE(
       SAFE_ADD(
         SAFE_ADD(
           SAFE_ADD(
             COALESCE(
               SAFE_MULTIPLY(
                 SAFE_CAST(REGEXP_EXTRACT(video_duration, r'P(\\d+)D') AS INT64),
                 86400
               ),
               0
             ),
             COALESCE(
               SAFE_MULTIPLY(
                 SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\\d+)H') AS INT64),
                 3600
               ),
               0
             )
           ),
           COALESCE(
             SAFE_MULTIPLY(
               SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\\d+)M') AS INT64),
               60
             ),
             0
           )
         ),
         COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\\d+)S') AS INT64), 0)
       ),
       0
     ) AS duration_sec"""

    assert re.sub(r"\s+", "", duration_expression) in re.sub(r"\s+", "", script)
