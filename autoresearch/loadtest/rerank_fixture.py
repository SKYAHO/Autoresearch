"""리랭킹 서빙 부하측정용 Feast 원본 fixture와 안전한 BigQuery DML renderer.

[파이프라인] 수집 → 웨어하우스 적재 → 피처 → 학습 → 일일 추천 → 노출 조립 →
LLM 판정 → action log → 재학습 흐름 중, 리랭킹 서빙 부하측정 전에 BigQuery
offline feature source를 결정론적으로 준비하는 구간을 담당한다.

[기능] 네 Feast source table에 필요한 고정 fixture 행을 생성하고, 그 행의 entity
키만 대상으로 하는 DELETE 및 명시적 컬럼 INSERT SQL을 만든다.

[비책임] Feast materialize와 Redis online store 갱신(feature_repo/ 및 Airflow),
HTTP 리랭킹 요청(src/serving/)은 이 모듈이 수행하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime
import re
from typing import Final, Mapping

from google.cloud import bigquery


FIXTURE_VERSION: Final[str] = "rerank-v1"
FIXTURE_USER_ID: Final[str] = "loadtest-user-001"
FIXTURE_VIDEO_IDS: Final[tuple[str, ...]] = tuple(
    f"loadtest-video-{index:03d}" for index in range(1, 201)
)
FIXTURE_CATEGORY_IDS: Final[tuple[str, ...]] = ("10", "20", "22", "24", "25")

_ALLOWED_TABLES: Final[frozenset[str]] = frozenset(
    {
        "user_static_feature",
        "user_dynamic_feature",
        "video_feature",
        "user_category_similarity",
    }
)
_PROJECT_IDENTIFIER = re.compile(r"^[a-z][a-z0-9-]{4,28}[a-z0-9]$")
_DATASET_IDENTIFIER = re.compile(r"^[A-Za-z0-9_]{1,1024}$")


@dataclass(frozen=True, slots=True)
class FixtureTable:
    """BigQuery source table 한 곳의 고정 fixture 행 모음."""

    name: str
    entity_column: str
    rows: tuple[Mapping[str, object], ...]


def build_fixture(timestamp: datetime) -> tuple[FixtureTable, ...]:
    """주어진 UTC 초 단위 시각으로 네 FeatureView fixture를 생성한다.

    Args:
        timestamp: 모든 fixture 행에 쓸 UTC 초 단위 event timestamp.

    Returns:
        user static/dynamic, video, similarity 순서의 고정 테이블 행.

    Raises:
        ValueError: timestamp가 UTC가 아니거나 microsecond를 포함할 때.
    """
    _validate_fixture_timestamp(timestamp)
    static_rows: tuple[Mapping[str, object], ...] = (
        {
            "user_id": FIXTURE_USER_ID,
            "event_timestamp": timestamp,
            "age_group": "30s",
            "occupation": "engineer",
            "preferred_category": ["10", "20", "22"],
            "preferred_topics": ["technology", "engineering", "science"],
            "watch_time_band": "medium",
        },
    )
    dynamic_rows: tuple[Mapping[str, object], ...] = (
        {
            "user_id": FIXTURE_USER_ID,
            "event_timestamp": timestamp,
            "recent_click_count_7d": 50,
            "recent_view_count_7d": 500,
            "recent_watch_time_7d": 36000,
            "recent_like_count_7d": 25,
            "historical_category_affinity": "10",
            "total_event_count_7d": 575,
        },
    )
    video_rows = tuple(
        {
            "video_id": video_id,
            "event_timestamp": timestamp,
            "category_id": FIXTURE_CATEGORY_IDS[(index - 1) % len(FIXTURE_CATEGORY_IDS)],
            "duration_sec": 60 + index,
            "view_count": 100000 + index * 100,
            "like_ratio": 0.05,
            "comment_ratio": 0.005,
            "days_since_upload": index % 365,
            "channel_subscriber_count": 1000000,
            "channel_view_count": 50000000,
            "channel_video_count": 1000,
        }
        for index, video_id in enumerate(FIXTURE_VIDEO_IDS, start=1)
    )
    similarity_rows = tuple(
        {
            "user_id": FIXTURE_USER_ID,
            "category_id": category_id,
            "event_timestamp": timestamp,
            "topic_similarity": index / 10,
            "topic_similarity_top_topic": "technology",
            "embedding_model": "text-multilingual-embedding-002",
            "embedding_dim": 768,
            "user_topic_embedding_version": FIXTURE_VERSION,
            "category_embedding_version": FIXTURE_VERSION,
            "similarity_method": "cosine",
            "similarity_pooling": "max",
        }
        for index, category_id in enumerate(FIXTURE_CATEGORY_IDS, start=1)
    )
    return (
        FixtureTable("user_static_feature", "user_id", static_rows),
        FixtureTable("user_dynamic_feature", "user_id", dynamic_rows),
        FixtureTable("video_feature", "video_id", video_rows),
        FixtureTable("user_category_similarity", "user_id", similarity_rows),
    )


def targeted_delete_sql(
    project: str, dataset: str, table: FixtureTable
) -> tuple[str, bigquery.QueryJobConfig]:
    """고정 fixture entity만 삭제하는 parameterized DML을 만든다."""
    table_id, validated_table = _validated_table_id(project, dataset, table)
    if validated_table.entity_column == "video_id":
        parameter = bigquery.ArrayQueryParameter(
            "video_ids", "STRING", list(FIXTURE_VIDEO_IDS)
        )
        predicate = "video_id IN UNNEST(@video_ids)"
    else:
        parameter = bigquery.ScalarQueryParameter(
            "user_id", "STRING", FIXTURE_USER_ID
        )
        predicate = "user_id = @user_id"
    sql = f"DELETE FROM `{table_id}`\nWHERE {predicate}"
    return sql, bigquery.QueryJobConfig(query_parameters=[parameter])


def targeted_count_sql(
    project: str, dataset: str, table: FixtureTable
) -> tuple[str, bigquery.QueryJobConfig]:
    """고정 fixture entity의 현재 행 수만 읽는 parameterized SQL을 만든다."""
    delete_sql, config = targeted_delete_sql(project, dataset, table)
    table_id = delete_sql.split("`")[1]
    predicate = delete_sql.partition("\nWHERE ")[2]
    return f"SELECT COUNT(*) AS row_count FROM `{table_id}`\nWHERE {predicate}", config


def targeted_insert_sql(project: str, dataset: str, table: FixtureTable) -> str:
    """검증된 고정 fixture 행을 명시적 source-table 컬럼으로 삽입하는 DML을 만든다."""
    table_id, validated_table = _validated_table_id(project, dataset, table)
    columns = tuple(validated_table.rows[0].keys())
    values = ",\n".join(
        "  (" + ", ".join(_literal(row[column]) for column in columns) + ")"
        for row in validated_table.rows
    )
    return f"INSERT INTO `{table_id}` ({', '.join(columns)})\nVALUES\n{values}"


def _validated_table_id(project: str, dataset: str, table: FixtureTable) -> tuple[str, FixtureTable]:
    _validate_identifier(project, _PROJECT_IDENTIFIER, "project")
    _validate_identifier(dataset, _DATASET_IDENTIFIER, "dataset")
    if table.name not in _ALLOWED_TABLES:
        raise ValueError(f"허용되지 않은 fixture table: {table.name}")
    expected_tables = {candidate.name: candidate for candidate in _fixture_for_table(table)}
    expected = expected_tables[table.name]
    if table != expected:
        raise ValueError("module-generated fixed fixture table만 DML로 렌더링할 수 있습니다")
    return f"{project}.{dataset}.{table.name}", expected


def _fixture_for_table(table: FixtureTable) -> tuple[FixtureTable, ...]:
    timestamp = table.rows[0].get("event_timestamp") if table.rows else None
    if not isinstance(timestamp, datetime):
        raise ValueError("fixture table에 event_timestamp가 필요합니다")
    return build_fixture(timestamp)


def _validate_fixture_timestamp(timestamp: datetime) -> None:
    if timestamp.tzinfo is None or timestamp.utcoffset() != UTC.utcoffset(timestamp):
        raise ValueError("fixture timestamp는 UTC timezone-aware datetime이어야 합니다")
    if timestamp.microsecond:
        raise ValueError("fixture timestamp는 second precision이어야 합니다")


def _validate_identifier(value: str, pattern: re.Pattern[str], name: str) -> None:
    if not pattern.fullmatch(value):
        raise ValueError(f"유효하지 않은 BigQuery {name} identifier")


def _literal(value: object) -> str:
    if isinstance(value, datetime):
        return "TIMESTAMP '" + value.strftime("%Y-%m-%d %H:%M:%S UTC") + "'"
    if isinstance(value, str):
        return "'" + value.replace("'", "''") + "'"
    if isinstance(value, list) and all(isinstance(item, str) for item in value):
        return "[" + ", ".join(_literal(item) for item in value) + "]"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return repr(value)
    raise TypeError(f"지원하지 않는 fixture literal type: {type(value).__name__}")
