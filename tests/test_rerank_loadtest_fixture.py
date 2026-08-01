"""리랭킹 부하테스트 fixture의 결정성 및 BigQuery DML 안전성 계약을 검증한다."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts import provision_rerank_loadtest_fixture as provisioner
from autoresearch.loadtest.rerank_fixture import (
    FIXTURE_USER_ID,
    FIXTURE_VIDEO_IDS,
    build_fixture,
    targeted_delete_sql,
    targeted_insert_sql,
)


class _FakeQueryJob:
    def __init__(self, sql: str) -> None:
        self._sql = sql
        self.num_dml_affected_rows = 1

    def result(self) -> list[SimpleNamespace] | _FakeQueryJob:
        if self._sql.startswith("SELECT"):
            return [SimpleNamespace(row_count=3)]
        return self


class _FakeBigQueryClient:
    def __init__(self, project: str) -> None:
        self.project = project
        self.calls: list[tuple[str, object]] = []

    def query(self, sql: str, job_config: object = None) -> _FakeQueryJob:
        self.calls.append((sql, job_config))
        return _FakeQueryJob(sql)


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


@pytest.mark.parametrize("project", ["Project-1", "short", "-project-1", "project-"])
def test_dml_rejects_non_gcp_project_identifier(project: str) -> None:
    """GCP project ID가 아닌 식별자는 BigQuery DML에 쓸 수 없다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[0]

    with pytest.raises(ValueError):
        targeted_delete_sql(project, "feast_offline_store", table)


def test_dml_accepts_bigquery_dataset_identifier_starting_with_number() -> None:
    """BigQuery dataset ID는 숫자로 시작해도 문자·숫자·밑줄만 쓰면 유효하다."""
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[0]

    sql, _ = targeted_delete_sql("project-1", "1_loadtest", table)

    assert "`project-1.1_loadtest.user_static_feature`" in sql


def test_k6_script_has_warmup_and_measurement_contract() -> None:
    """k6는 warmup을 분리하고 측정 전용 오류율을 노출해야 한다."""
    script = Path("loadtest/rerank.js").read_text()

    assert 'exec: "warmup"' in script
    assert 'exec: "measure"' in script
    assert "rerank_measure_duration_seconds" in script
    assert "rerank_measure_failure" in script
    assert "rate<0.01" in script
    assert "loadtest-user-001" in script
    assert "loadtest-video-200" in script
    assert 'new Trend("rerank_measure_duration_seconds")' in script
    assert 'new Trend("rerank_measure_duration_seconds", true)' not in script
    assert "response.timings.duration / 1000" in script
    for status_code in ("200", "422", "500", "503", "other"):
        assert f"rerank_measure_status_code_{status_code}" in script


def test_k6_job_has_no_identity_or_token_mount() -> None:
    """k6 Job은 전용 KSA만 쓰고 토큰·Secret·권한 상승을 허용하지 않는다."""
    text = Path("deploy/loadtest/rerank-k6-job.yaml").read_text()

    assert "serviceAccountName: rerank-loadtest" in text
    assert "automountServiceAccountToken: false" in text
    assert "restartPolicy: Never" in text
    assert "allowPrivilegeEscalation: false" in text
    assert "readOnlyRootFilesystem: true" in text
    assert "REDIS_" not in text and "secretKeyRef:" not in text


def test_k6_job_is_immutable_hardened_and_configmap_only() -> None:
    """k6 Job은 고정 digest와 one-shot 제한을 쓰며 두 ConfigMap만 mount한다."""
    text = Path("deploy/loadtest/rerank-k6-job.yaml").read_text()

    assert "generateName: rerank-k6-" in text
    assert "namespace: loadtest" in text
    assert "app.kubernetes.io/part-of: rerank-loadtest" in text
    assert "backoffLimit: 0" in text
    assert "activeDeadlineSeconds: 600" in text
    assert "ttlSecondsAfterFinished: 86400" in text
    assert (
        "grafana/k6@sha256:1f40432b1cbe7234e977f96c362c9bc5"
        "50a2d2b583d014dd8669fe40d3e9e755" in text
    )
    assert "k6 run" in text and "/scripts/rerank.js" in text
    assert "runAsNonRoot: true" in text
    assert "drop:" in text and "- ALL" in text
    assert "type: RuntimeDefault" in text
    assert text.count("configMap:") == 2
    for forbidden in (
        "hostPath:",
        "persistentVolumeClaim:",
        "projected:",
        "serviceAccountToken:",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "DB_",
    ):
        assert forbidden not in text


def test_manual_workflow_keeps_load_and_snapshot_identities_separate() -> None:
    """수동 workflow는 VU gate와 Prometheus 조회를 서로 다른 identity로 실행한다."""
    text = Path(".github/workflows/rerank-loadtest.yml").read_text()

    assert "workflow_dispatch:" in text
    assert "candidate_count:" in text and "- 24" in text and "- 200" in text
    assert "benchmark_label:" in text
    assert "- baseline" in text and "- optimized" in text
    assert "fixture_version:" in text and "default: rerank-v1" in text
    assert "serving_image_ref:" in text and "serving_git_sha:" in text
    assert "for vus in 1 2 4 8" in text
    assert ".data.metrics.rerank_measure_failure.values.rate" in text
    assert "< 0.01" in text
    assert "RERANK_LOADTEST_RUNNER_SA" in text
    assert "RERANK_PROMETHEUS_SNAPSHOT_READER_SA" in text
    assert text.count("google-github-actions/auth@v2") == 2
    assert text.count("google-github-actions/get-gke-credentials@v2") == 2
    assert "k6-summary-" in text and "metadata-" in text
    assert "creation_timestamp" in text and "completion_timestamp" in text
    assert "PROMETHEUS_SERVICE_PROXY:" in text
    assert (
        "/api/v1/namespaces/monitoring/services/"
        "http:kube-prometheus-stack-prometheus:9090/proxy" in text
    )
    assert "/api/v1/query_range" in text
    assert "step=30" in text
    for query_name in (
        "phase_p50",
        "phase_p95",
        "outcome_rate",
        "max_in_flight",
        "cpu_seconds",
        "rss",
        "cfs_throttling",
    ):
        assert query_name in text
    assert ".status == \"success\"" in text
    assert "actions/upload-artifact@v4" in text


def test_manual_workflow_serializes_shared_configmaps_and_waits_for_padding() -> None:
    """공유 ConfigMap 실행은 직렬화하고 Prometheus 종료 패딩은 미래를 조회하지 않는다."""
    text = Path(".github/workflows/rerank-loadtest.yml").read_text()

    assert "group: rerank-loadtest\n" in text
    assert "padded_end=" in text
    assert "current_time=" in text
    assert 'sleep "$((padded_end - current_time))"' in text


def test_provisioner_default_dry_run_executes_only_count_selects(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """기본 CLI는 fixture 행 수만 읽고 어떠한 DML도 실행하지 않는다."""
    client = _FakeBigQueryClient("project-1")
    monkeypatch.setattr(provisioner.bigquery, "Client", lambda project: client)

    assert provisioner.main(["--project", "project-1"]) == 0

    assert len(client.calls) == 4
    assert all(sql.startswith("SELECT COUNT(*) AS row_count") for sql, _ in client.calls)
    assert all("DELETE FROM" not in sql and "INSERT INTO" not in sql for sql, _ in client.calls)
    assert all(config is not None for _, config in client.calls)


def test_provisioner_apply_executes_only_targeted_delete_and_insert(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """명시적 --apply는 네 fixture table의 DELETE/INSERT만 실행한다."""
    client = _FakeBigQueryClient("project-1")
    monkeypatch.setattr(provisioner.bigquery, "Client", lambda project: client)

    assert provisioner.main(["--project", "project-1", "--apply"]) == 0

    assert len(client.calls) == 8
    delete_sqls = [sql for sql, _ in client.calls if sql.startswith("DELETE FROM")]
    insert_sqls = [sql for sql, _ in client.calls if sql.startswith("INSERT INTO")]
    assert len(delete_sqls) == len(insert_sqls) == 4
    assert all("WHERE user_id = @user_id" in sql or "WHERE video_id IN UNNEST(@video_ids)" in sql for sql in delete_sqls)
    assert all("WRITE_TRUNCATE" not in sql and "CREATE OR REPLACE" not in sql for sql, _ in client.calls)
