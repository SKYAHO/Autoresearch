"""일일 추천 배치 헬퍼 단위 테스트 — 실 BQ 미접속(fake client)."""

from datetime import UTC, date, datetime

import pandas as pd

from src.pipeline.daily_recommendations import (
    RECOMMENDATIONS_SCHEMA,
    ensure_output_table,
    parse_iso8601_duration,
    to_recommendation_rows,
    write_partition,
)
from src.serving.schemas import RerankedVideo

_GENERATED_AT = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def _rows(ranked):
    return to_recommendation_rows(
        "vu_0001",
        ranked,
        dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        model_run_id="run-abc",
        model_version="3",
        generated_at=_GENERATED_AT,
    )


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT4M29S") == 269
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration(None) == 0
    assert parse_iso8601_duration("not-a-duration") == 0
    assert parse_iso8601_duration("PT1Mgarbage") == 0


def test_rows_rank_descending_with_video_id_tiebreak():
    ranked = [
        RerankedVideo(video_id="vB", ctr_score=0.9),
        RerankedVideo(video_id="vC", ctr_score=0.5),
        RerankedVideo(video_id="vA", ctr_score=0.9),  # vB와 동점 → video_id 오름차순으로 앞
    ]
    rows = _rows(ranked)
    assert [(r["rank"], r["video_id"]) for r in rows] == [(1, "vA"), (2, "vB"), (3, "vC")]
    first = rows[0]
    assert first["user_id"] == "vu_0001"
    assert first["dt"] == date(2026, 7, 21)
    assert first["model_run_id"] == "run-abc"
    assert first["model_version"] == "3"
    assert first["generated_at"] == _GENERATED_AT


class _FakeLoadJob:
    def result(self):
        return None


class _FakeClient:
    def __init__(self):
        self.created = []
        self.loads = []
        self.partitions = {}

    def create_table(self, table, exists_ok=False):
        self.created.append((table, exists_ok))

    def load_table_from_dataframe(self, frame, destination, job_config=None):
        self.loads.append((frame.copy(), destination, job_config))
        if job_config.write_disposition == "WRITE_TRUNCATE":
            self.partitions[destination] = frame.copy()
        return _FakeLoadJob()


def test_ensure_output_table_creates_partitioned_table_exists_ok():
    client = _FakeClient()
    ensure_output_table(client, "proj.ds.user_recommendations")
    (table, exists_ok), = client.created
    assert exists_ok is True
    assert table.time_partitioning.field == "dt"
    assert [f.name for f in table.schema] == [f.name for f in RECOMMENDATIONS_SCHEMA]


def test_write_partition_is_idempotent_by_truncate_decorator():
    client = _FakeClient()
    frame = pd.DataFrame({"user_id": ["u1"], "video_id": ["v1"]})
    for _ in range(2):  # 같은 dt 2회 실행 = 같은 파티션 대상 + TRUNCATE → 중복 불가능
        write_partition(client, "proj.ds.user_recommendations", frame, date(2026, 7, 21))
    destinations = [dest for _, dest, _ in client.loads]
    dispositions = [cfg.write_disposition for _, _, cfg in client.loads]
    assert destinations == ["proj.ds.user_recommendations$20260721"] * 2
    assert dispositions == ["WRITE_TRUNCATE"] * 2
    assert len(client.partitions["proj.ds.user_recommendations$20260721"]) == 1
