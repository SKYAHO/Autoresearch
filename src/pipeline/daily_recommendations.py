"""일일 추천 결과 BQ 적재 배치.

champion 모델(models:/ctr-model@champion)로 일일 트렌딩 후보 전체를 가상 유저
전원에 대해 채점해, 유저별 전체 순위를 user_recommendations 파티션 테이블에
멱등 적재한다. 비교 실험·노출 선정은 이 배치의 책임이 아니다(spec 참조).

spec: docs/specs/2026-07-21-daily-recommendations-batch.md
"""

from __future__ import annotations

__arch__ = {
    "stage": "training",
    "role": "champion 모델로 일일 후보를 채점해 유저별 순위를 BigQuery에 적재합니다.",
    "owns": [
        "일일 추천 순위 산출·계보 태깅",
        "user_recommendations 파티션 멱등 적재",
    ],
    "not_owns": [
        "노출 선정(Top-K + exploration)과 LLM 판정",
        "모델 학습과 Registry alias 운영",
    ],
}

import argparse
import json
import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from typing import Callable, Final, Sequence

import pandas as pd
from google.cloud import bigquery

from autoresearch.jobs import BATCH_CONTRACT_VERSION
from src.pipeline.build_training_dataset import (
    BIGQUERY_DATASET,
    BIGQUERY_PROJECT,
    derive_wide_events,
    load_events_from_bigquery,
)
from src.pipeline.virtual_user_adapter import to_personas_frame
from src.serving.model_loader import (
    RegistryModelSettings,
    ResolvedModel,
    load_reranker_with_lineage,
)
from src.serving.schemas import RerankedVideo
from src.pipeline.simulate_policy_round import _to_candidate_videos, build_pool_feature_frame

logger = logging.getLogger(__name__)
JOB_NAME: Final = "daily_recommendations"
_REVISION: Final = os.getenv("AUTORESEARCH_REVISION", "unknown")

_DURATION_PATTERN = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

RECOMMENDATIONS_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("dt", "DATE"),
    bigquery.SchemaField("user_id", "STRING"),
    bigquery.SchemaField("video_id", "STRING"),
    bigquery.SchemaField("rank", "INTEGER"),
    bigquery.SchemaField("ctr_score", "FLOAT"),
    bigquery.SchemaField("model_run_id", "STRING"),
    bigquery.SchemaField("model_version", "STRING"),
    bigquery.SchemaField("events_dt", "DATE"),
    bigquery.SchemaField("generated_at", "TIMESTAMP"),
]


def parse_iso8601_duration(value: object) -> int:
    """ISO8601 duration 문자열(PT4M29S)을 초로 변환한다. 해석 불가 값은 0."""
    if not isinstance(value, str):
        return 0
    match = _DURATION_PATTERN.fullmatch(value)
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds or 0)


def to_recommendation_rows(
    user_id: str,
    ranked: list[RerankedVideo],
    *,
    dt: date,
    events_dt: date,
    model_run_id: str,
    model_version: str | None,
    generated_at: datetime,
) -> list[dict]:
    """채점 결과를 결정론적 순위 행으로 조립한다(동점은 video_id 오름차순)."""
    ordered = sorted(ranked, key=lambda item: (-item.ctr_score, item.video_id))
    return [
        {
            "dt": dt,
            "user_id": user_id,
            "video_id": item.video_id,
            "rank": position,
            "ctr_score": float(item.ctr_score),
            "model_run_id": model_run_id,
            "model_version": model_version,
            "events_dt": events_dt,
            "generated_at": generated_at,
        }
        for position, item in enumerate(ordered, start=1)
    ]


def ensure_output_table(client: bigquery.Client, table_id: str) -> None:
    """출력 테이블이 없으면 dt DAY 파티션 스키마로 생성한다(있으면 무시)."""
    table = bigquery.Table(table_id, schema=RECOMMENDATIONS_SCHEMA)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="dt"
    )
    client.create_table(table, exists_ok=True)


def write_partition(
    client: bigquery.Client, table_id: str, frame: pd.DataFrame, dt: date
) -> None:
    """해당 날짜 파티션을 원자적으로 대체한다(재실행 멱등)."""
    destination = f"{table_id}${dt.strftime('%Y%m%d')}"
    job_config = bigquery.LoadJobConfig(
        schema=RECOMMENDATIONS_SCHEMA,
        write_disposition="WRITE_TRUNCATE",
    )
    client.load_table_from_dataframe(frame, destination, job_config=job_config).result()
