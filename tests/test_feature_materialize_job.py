import json
import re
from pathlib import Path
from unittest.mock import MagicMock, call

import pytest

import autoresearch.jobs.feature_materialize as feature_materialize


def _summary(output: str) -> dict[str, object]:
    return json.loads(output.splitlines()[-1])


def test_main_runs_each_feature_table_in_order(monkeypatch, capsys):
    client = MagicMock()
    query_jobs = [MagicMock(job_id=f"job-{index}") for index in range(3)]
    for index, job in enumerate(query_jobs):
        job.result.return_value = [{"final_row_count": index + 1}]
    client.query.side_effect = query_jobs
    monkeypatch.setattr(
        feature_materialize, "_bigquery_client", lambda project_id: client
    )

    assert (
        feature_materialize.main(
            [
                "--project",
                "test-project",
                "--dataset",
                "test_dataset",
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 0
    )

    expected_scripts = [
        feature_materialize.build_materialize_script(
            "test-project", "test_dataset", "raw_dataset", table_name
        )
        for table_name in feature_materialize.FEATURE_TABLES
    ]
    assert client.query.call_args_list == [call(script) for script in expected_scripts]
    assert [job.result.call_count for job in query_jobs] == [1, 1, 1]
    summary = _summary(capsys.readouterr().out)
    assert summary["status"] == "succeeded"
    assert summary["tables"] == list(feature_materialize.FEATURE_TABLES)
    assert summary["job_ids"] == ["job-0", "job-1", "job-2"]
    assert summary["row_counts"] == {
        "user_static_feature": 1,
        "user_dynamic_feature": 2,
        "video_feature": 3,
    }


@pytest.mark.parametrize("result", [[], [{}], [{"final_row_count": "3"}]])
def test_main_stops_when_final_row_count_is_missing_or_not_an_integer(
    result, monkeypatch, caplog, capsys
):
    client = MagicMock()
    first_job = MagicMock(job_id="job-static")
    first_job.result.return_value = result
    client.query.return_value = first_job
    monkeypatch.setattr(
        feature_materialize, "_bigquery_client", lambda project_id: client
    )

    assert (
        feature_materialize.main(
            [
                "--project",
                "test-project",
                "--dataset",
                "test_dataset",
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 1
    )

    assert client.query.call_count == 1
    assert _summary(capsys.readouterr().out)["error_type"] == "runtime_failure"
    assert "final_row_count" not in caplog.text


def test_main_stops_when_a_table_query_fails(monkeypatch, capsys):
    client = MagicMock()
    first_job = MagicMock(job_id="job-static")
    first_job.result.return_value = [{"final_row_count": 1}]
    client.query.side_effect = [first_job, RuntimeError("query failed")]
    monkeypatch.setattr(
        feature_materialize, "_bigquery_client", lambda project_id: client
    )

    assert (
        feature_materialize.main(
            [
                "--project",
                "test-project",
                "--dataset",
                "test_dataset",
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 1
    )

    first_script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", "user_static_feature"
    )
    second_script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", "user_dynamic_feature"
    )
    assert client.query.call_args_list == [call(first_script), call(second_script)]
    first_job.result.assert_called_once_with()
    assert _summary(capsys.readouterr().out)["error_type"] == "runtime_failure"


def test_main_stops_when_a_table_result_fails(monkeypatch, capsys):
    client = MagicMock()
    first_job = MagicMock(job_id="job-static")
    first_job.result.return_value = [{"final_row_count": 1}]
    second_job = MagicMock(job_id="job-dynamic")
    second_job.result.side_effect = RuntimeError("result failed")
    client.query.side_effect = [first_job, second_job]
    monkeypatch.setattr(
        feature_materialize, "_bigquery_client", lambda project_id: client
    )

    assert (
        feature_materialize.main(
            [
                "--project",
                "test-project",
                "--dataset",
                "test_dataset",
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 1
    )

    assert client.query.call_count == 2
    first_job.result.assert_called_once_with()
    second_job.result.assert_called_once_with()
    assert _summary(capsys.readouterr().out)["error_type"] == "runtime_failure"


def test_main_rejects_invalid_project_identifier(monkeypatch, capsys):
    monkeypatch.setattr(
        feature_materialize, "_run", lambda args: pytest.fail("must not run")
    )

    assert (
        feature_materialize.main(
            [
                "--project",
                "bad project",
                "--dataset",
                "dataset",
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 2
    )

    assert _summary(capsys.readouterr().out)["error_type"] == "invalid_arguments"


def test_main_requires_raw_dataset(monkeypatch, capsys):
    monkeypatch.setattr(
        feature_materialize, "_run", lambda args: pytest.fail("must not run")
    )

    assert (
        feature_materialize.main(
            ["--project", "test-project", "--dataset", "feature_dataset"]
        )
        == 2
    )

    assert _summary(capsys.readouterr().out)["error_type"] == "invalid_arguments"


def test_main_summary_records_raw_dataset(monkeypatch, capsys):
    client = MagicMock()
    query_jobs = [MagicMock(job_id=f"job-{index}") for index in range(3)]
    for index, job in enumerate(query_jobs):
        job.result.return_value = [{"final_row_count": index + 1}]
    client.query.side_effect = query_jobs
    monkeypatch.setattr(
        feature_materialize, "_bigquery_client", lambda project_id: client
    )

    assert (
        feature_materialize.main(
            [
                "--project",
                "test-project",
                "--dataset",
                "feature_dataset",
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 0
    )

    summary = _summary(capsys.readouterr().out)
    assert summary["dataset"] == "feature_dataset"
    assert summary["raw_dataset"] == "raw_dataset"


def test_main_accepts_gcp_project_id(monkeypatch, capsys):
    monkeypatch.setattr(
        feature_materialize,
        "_run",
        lambda args: {"status": "succeeded"},
    )

    assert (
        feature_materialize.main(
            [
                "--project",
                "ar-infra-501607",
                "--dataset",
                "test_dataset",
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 0
    )

    assert _summary(capsys.readouterr().out)["status"] == "succeeded"


@pytest.mark.parametrize(
    "project_id",
    [
        "_project",
        "Project-id",
        "abcde",
        "a" * 31,
        "project-",
    ],
)
def test_main_rejects_invalid_gcp_project_id_before_running(
    project_id, monkeypatch, caplog, capsys
):
    monkeypatch.setattr(
        feature_materialize, "_run", lambda args: pytest.fail("must not run")
    )

    assert (
        feature_materialize.main(
            [
                "--project",
                project_id,
                "--dataset",
                "test_dataset",
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 2
    )

    output = capsys.readouterr().out
    assert _summary(output)["error_type"] == "invalid_arguments"
    assert project_id not in output
    assert project_id not in caplog.text


def test_main_accepts_maximum_length_bigquery_dataset_id(monkeypatch, capsys):
    run = MagicMock(return_value={"status": "succeeded"})
    monkeypatch.setattr(feature_materialize, "_run", run)

    assert (
        feature_materialize.main(
            [
                "--project",
                "test-project",
                "--dataset",
                "a" * 1024,
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 0
    )

    run.assert_called_once()
    assert _summary(capsys.readouterr().out)["status"] == "succeeded"


def test_main_rejects_oversized_bigquery_dataset_id_before_running(
    monkeypatch, caplog, capsys
):
    dataset_id = "a" * 1025
    monkeypatch.setattr(
        feature_materialize, "_run", lambda args: pytest.fail("must not run")
    )

    assert (
        feature_materialize.main(
            [
                "--project",
                "test-project",
                "--dataset",
                dataset_id,
                "--raw-dataset",
                "raw_dataset",
            ]
        )
        == 2
    )

    output = capsys.readouterr().out
    assert _summary(output)["error_type"] == "invalid_arguments"
    assert dataset_id not in output
    assert dataset_id not in caplog.text


def test_feature_tables_are_the_three_supported_sources():
    assert feature_materialize.FEATURE_TABLES == (
        "user_static_feature",
        "user_dynamic_feature",
        "video_feature",
    )


@pytest.mark.parametrize(
    ("table_name", "columns"),
    [
        (
            "user_static_feature",
            (
                "user_id",
                "event_timestamp",
                "age_group",
                "occupation",
                "preferred_category",
                "preferred_topics",
                "watch_time_band",
            ),
        ),
        (
            "user_dynamic_feature",
            (
                "user_id",
                "event_timestamp",
                "recent_click_count_7d",
                "recent_view_count_7d",
                "recent_watch_time_7d",
                "recent_like_count_7d",
                "historical_category_affinity",
                "total_event_count_7d",
            ),
        ),
        (
            "video_feature",
            (
                "video_id",
                "event_timestamp",
                "category_id",
                "duration_sec",
                "view_count",
                "like_ratio",
                "comment_ratio",
                "days_since_upload",
                "channel_subscriber_count",
                "channel_view_count",
                "channel_video_count",
            ),
        ),
    ],
)
def test_script_inserts_explicit_columns_in_target_schema_order(table_name, columns):
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", table_name
    )
    column_list = ",\n  ".join(columns)
    expected_insert = (
        f"INSERT INTO `test-project.test_dataset.{table_name}` (\n"
        f"  {column_list}\n"
        ")\n"
        "SELECT\n"
        f"  {column_list}\n"
        "FROM materialized_rows;"
    )

    assert expected_insert in script


def test_dynamic_script_separates_raw_and_feature_datasets():
    script = feature_materialize.build_materialize_script(
        "test-project",
        "feature_dataset",
        "raw_dataset",
        "user_dynamic_feature",
    )

    assert "`test-project.raw_dataset.data_lake_action_log`" in script
    assert "`test-project.raw_dataset.data_lake_youtube_trending_kr`" in script
    assert "DELETE FROM `test-project.feature_dataset.user_dynamic_feature`" in script


def test_static_script_reads_virtual_user_source_from_raw_dataset():
    script = feature_materialize.build_materialize_script(
        "test-project",
        "feature_dataset",
        "raw_dataset",
        "user_static_feature",
    )

    assert "`test-project.raw_dataset.asset_virtual_user_vu_1000`" in script
    assert "feature_dataset.asset_virtual_user_vu_1000" not in script
    assert "DELETE FROM `test-project.feature_dataset.user_static_feature`" in script


def test_static_script_flattens_bigquery_parquet_list_wrappers():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", "user_static_feature"
    )

    assert "UNNEST(primary_categories.list) AS item" in script
    assert "item.element" in script
    assert "ARRAY<STRING>[]" in script
    assert "asset_virtual_user_vu_1000" in script


def test_static_script_flattens_every_virtual_user_list_column():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", "user_static_feature"
    )

    for column_name in (
        "primary_categories",
        "hobby_keywords",
        "interest_keywords",
        "lifestyle_keywords",
        "food_keywords",
        "travel_keywords",
        "career_keywords",
        "family_context_keywords",
    ):
        assert f"UNNEST({column_name}.list) AS item" in script


@pytest.mark.parametrize(
    "table_name,raw_table",
    [
        ("user_dynamic_feature", "data_lake_action_log"),
        ("video_feature", "data_lake_youtube_trending_kr"),
    ],
)
def test_supported_script_references_its_raw_source(table_name, raw_table):
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", table_name
    )

    assert raw_table in script
    assert "BEGIN TRANSACTION" in script
    assert "DELETE FROM" in script
    assert "INSERT INTO" in script
    assert "ASSERT" in script
    assert "COMMIT TRANSACTION;\nSELECT COUNT(*) AS final_row_count FROM" in script
    assert "CREATE OR REPLACE TABLE" not in script


def test_script_rejects_unknown_feature_table():
    table_name = "user_category_similarity; secret-value"

    with pytest.raises(ValueError, match="^unsupported feature table$") as error:
        feature_materialize.build_materialize_script(
            "test-project", "test_dataset", "raw_dataset", table_name
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
            project_id, dataset_id, "raw_dataset", "user_static_feature"
        )

    assert project_id not in str(error.value)
    assert dataset_id not in str(error.value)


@pytest.mark.parametrize("project_id", [None, 1, object()])
def test_script_rejects_non_string_project_identifier(project_id):
    with pytest.raises(ValueError, match="^invalid project_id$"):
        feature_materialize.build_materialize_script(
            project_id, "test_dataset", "raw_dataset", "user_static_feature"
        )


@pytest.mark.parametrize("dataset_id", [None, 1, object()])
def test_script_rejects_non_string_dataset_identifier(dataset_id):
    with pytest.raises(ValueError, match="^invalid dataset_id$"):
        feature_materialize.build_materialize_script(
            "test-project", dataset_id, "raw_dataset", "user_static_feature"
        )


def test_video_script_uses_single_backslash_iso_8601_duration_patterns():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", "video_feature"
    )

    for pattern in (r"P(\d+)D", r"(\d+)H", r"(\d+)M", r"(\d+)S"):
        assert f"r'{pattern}'" in script


def test_video_script_uses_safe_duration_arithmetic():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", "video_feature"
    )

    assert "SAFE_MULTIPLY(" in script
    assert "SAFE_ADD(" in script
    assert re.search(r"COALESCE\(\s*SAFE_MULTIPLY\(", script)


def test_video_script_duration_contract_composes_pt1d2h3m4s_as_93784_seconds():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", "video_feature"
    )

    expected_duration_sec = 1 * 86400 + 2 * 3600 + 3 * 60 + 4

    assert expected_duration_sec == 93784
    assert "SAFE_MULTIPLY(" in script
    assert "86400" in script
    assert "3600" in script
    assert "60" in script
    assert (
        "COALESCE(SAFE_CAST(REGEXP_EXTRACT(video_duration, r'(\\d+)S') AS INT64), 0)"
        in script
    )


def test_video_script_duration_contract_propagates_safe_add_overflow_to_outer_coalesce():
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", "video_feature"
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


def test_video_duration_expression_in_guide_matches_generated_builder_sql():
    guide = (
        Path(__file__).parents[1] / "docs" / "guides" / "data-warehouse.md"
    ).read_text()
    script = feature_materialize.build_materialize_script(
        "test-project", "test_dataset", "raw_dataset", "video_feature"
    )

    def duration_expression(sql: str) -> str:
        start = sql.index("COALESCE(", sql.index("video_category AS category_id,"))
        end = sql.index(") AS duration_sec,", start) + len(") AS duration_sec")
        return re.sub(r"\s+", "", sql[start:end])

    assert duration_expression(guide) == duration_expression(script)
