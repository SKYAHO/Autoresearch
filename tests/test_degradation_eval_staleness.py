"""video feature staleness 측정 테스트 (#471, spec §4).

`days_since_upload`(콘텐츠 나이)와 feature staleness(선택된 스냅샷의 오래됨)는 다른
값이다 — 이 테스트는 후자만 다룬다. PIT join이 실제로 고른 `video_feature` 행의
`event_timestamp`는 학습 조립 경로(get_historical_features)가 노출하지 않으므로,
진단 전용 별도 BigQuery 조회로 확보한다(SQL 구성은 순수 함수로 분리해 검증한다).
"""

import sys
from pathlib import Path

import pandas as pd
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from autoresearch.model_evaluation.degradation_eval import (  # noqa: E402
    VideoStalenessStatus,
    _resolve_staleness_summary,
    compute_video_staleness_summary,
    resolve_video_feature_snapshot_timestamps,
    video_feature_snapshot_query,
)


def test_video_feature_snapshot_query_targets_video_feature_table():
    sql, job_config = video_feature_snapshot_query(
        project="proj", dataset="ds", video_ids=["v1", "v2"], as_of="2026-07-21 00:00:00"
    )

    assert "proj.ds.video_feature" in sql
    assert "MAX(event_timestamp)" in sql
    # TIMESTAMP(string) 1-인자 형태는 BigQuery가 UTC로 해석한다 — KST 자정을
    # 의도했다면 2-인자 형태로 타임존을 명시해야 한다(PR #510 리뷰, 9시간 오차 수정).
    assert "TIMESTAMP(@as_of, 'Asia/Seoul')" in sql

    param_names = {p.name for p in job_config.query_parameters}
    assert param_names == {"video_ids", "as_of"}


class _FakeQueryJob:
    def __init__(self, frame: pd.DataFrame):
        self._frame = frame

    def to_dataframe(self) -> pd.DataFrame:
        return self._frame.copy()


class _FakeBigQueryClient:
    def __init__(self, frame: pd.DataFrame):
        self._frame = frame
        self.queries = []

    def query(self, sql, job_config=None):
        self.queries.append((sql, job_config))
        return _FakeQueryJob(self._frame)


def test_resolve_video_feature_snapshot_timestamps_maps_by_video_id():
    frame = pd.DataFrame(
        {
            "video_id": ["v1", "v2"],
            "selected_ts": pd.to_datetime(["2026-07-18", "2026-07-20"], utc=True),
        }
    )
    client = _FakeBigQueryClient(frame)

    resolved = resolve_video_feature_snapshot_timestamps(
        client, project="proj", dataset="ds", video_ids=["v1", "v2", "v3"], as_of="2026-07-21"
    )

    # v3는 조회 결과에 없음 = 그 시점 이전 스냅샷을 못 찾음(cold-start와 자연히 일치).
    assert resolved.keys() == {"v1", "v2"}
    assert resolved["v1"] == pd.Timestamp("2026-07-18", tz="UTC")


def test_compute_video_staleness_summary_averages_resolved_ages():
    dataset = pd.DataFrame(
        {
            "video_id": ["v1", "v2"],
            "event_timestamp": pd.to_datetime(["2026-07-21", "2026-07-21"], utc=True),
        }
    )
    snapshot_timestamps = {
        "v1": pd.Timestamp("2026-07-20", tz="UTC"),  # age 1일
        "v2": pd.Timestamp("2026-07-11", tz="UTC"),  # age 10일
    }

    summary = compute_video_staleness_summary(dataset, snapshot_timestamps)

    assert summary.status == VideoStalenessStatus.AVAILABLE
    assert summary.mean_age_days == pytest.approx(5.5)
    assert summary.max_age_days == pytest.approx(10.0)
    assert summary.resolved_count == 2
    assert summary.unresolved_count == 0


def test_compute_video_staleness_summary_excludes_unresolved_videos():
    # v2는 cold-start(스냅샷 미발견)라 age 계산에서 빠져야 한다 — 섞으면 왜곡된다.
    dataset = pd.DataFrame(
        {
            "video_id": ["v1", "v2"],
            "event_timestamp": pd.to_datetime(["2026-07-21", "2026-07-21"], utc=True),
        }
    )
    snapshot_timestamps = {"v1": pd.Timestamp("2026-07-20", tz="UTC")}

    summary = compute_video_staleness_summary(dataset, snapshot_timestamps)

    assert summary.status == VideoStalenessStatus.AVAILABLE
    assert summary.mean_age_days == pytest.approx(1.0)
    assert summary.max_age_days == pytest.approx(1.0)
    assert summary.resolved_count == 1
    assert summary.unresolved_count == 1


def test_compute_video_staleness_summary_unavailable_when_nothing_resolved():
    dataset = pd.DataFrame(
        {
            "video_id": ["v1"],
            "event_timestamp": pd.to_datetime(["2026-07-21"], utc=True),
        }
    )

    summary = compute_video_staleness_summary(dataset, snapshot_timestamps={})

    assert summary.status == VideoStalenessStatus.UNAVAILABLE
    assert summary.mean_age_days is None
    assert summary.max_age_days is None
    assert summary.resolved_count == 0
    assert summary.unresolved_count == 1
    assert summary.reason is not None


def test_resolve_staleness_summary_degrades_gracefully_when_entity_columns_missing():
    # 실제 평가일 CSV 스키마(MODEL_FEATURE_COLUMNS + clicked)는 video_id/event_timestamp를
    # 담지 않는다(build_training_dataset.py의 조립 계약) — client가 있어도 KeyError로
    # 죽지 않고 UNAVAILABLE로 떨어져야 한다(PR #510 리뷰 High).
    dataset = pd.DataFrame({"category_id": ["a", "b"], "clicked": [1, 0]})
    client = _FakeBigQueryClient(pd.DataFrame({"video_id": [], "selected_ts": []}))

    summary = _resolve_staleness_summary(
        dataset,
        "2026-07-21",
        bigquery_client=client,
        bigquery_project="proj",
        bigquery_dataset="ds",
    )

    assert summary.status == VideoStalenessStatus.UNAVAILABLE
    assert "video_id" in summary.reason
    assert "event_timestamp" in summary.reason
    assert client.queries == []  # 컬럼이 없으면 조회 자체를 시도하지 않는다


def test_resolve_staleness_summary_unavailable_without_client():
    dataset = pd.DataFrame(
        {
            "video_id": ["v1"],
            "event_timestamp": pd.to_datetime(["2026-07-21"], utc=True),
        }
    )

    summary = _resolve_staleness_summary(
        dataset, "2026-07-21", bigquery_client=None, bigquery_project=None, bigquery_dataset=None
    )

    assert summary.status == VideoStalenessStatus.UNAVAILABLE
