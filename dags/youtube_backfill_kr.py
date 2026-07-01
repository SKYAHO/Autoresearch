"""One-shot backfill: Kaggle global parquet -> KR dt partitions.

Manual DAG (``schedule=None``). Trigger with Params to override the source
parquet path / output bucket:

  * ``source_path`` - global Kaggle parquet (``gs://...`` or local path)
  * ``base_path``   - lake dir, bucket-relative (e.g. ``bucket/data_lake/...``)

Falls back to ``YOUTUBE_BACKFILL_SOURCE`` / ``YOUTUBE_LAKE_BUCKET`` env/Variable.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airflow.decorators import dag, task
from airflow.models import Variable

from autoresearch.youtube_collection.backfill import backfill_from_parquet

logger = logging.getLogger(__name__)

LAKE_DIR_NAME = "data_lake/youtube_trending_kr"


def _get_config(name: str, default: str | None = None) -> str | None:
    value = os.environ.get(name)
    if value:
        return value
    try:
        return Variable.get(name, default_var=default)
    except Exception:
        return default


def _gcs_filesystem():
    import pyarrow.fs as fs

    return fs.GcsFileSystem()


@dag(
    dag_id="youtube_backfill_kr",
    schedule=None,
    start_date=datetime(2026, 6, 25),
    catchup=False,
    tags=["youtube", "collection", "kr", "backfill"],
    params={
        "source_path": "",
        "base_path": "",
    },
)
def youtube_backfill_kr():
    @task
    def run(**context) -> int:
        params = context.get("params") or {}
        source_path = params.get("source_path") or _get_config(
            "YOUTUBE_BACKFILL_SOURCE"
        )
        base_path = params.get("base_path")
        if not base_path:
            bucket = _get_config("YOUTUBE_LAKE_BUCKET")
            if not bucket:
                raise RuntimeError(
                    "base_path param or YOUTUBE_LAKE_BUCKET is not set"
                )
            base_path = f"{bucket}/{LAKE_DIR_NAME}"
        if not source_path:
            raise RuntimeError(
                "source_path param or YOUTUBE_BACKFILL_SOURCE is not set"
            )

        total = backfill_from_parquet(source_path, base_path)
        logger.info("Backfill complete: %d rows -> %s", total, base_path)
        return total

    run()


youtube_backfill_kr()
