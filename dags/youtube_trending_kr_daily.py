"""Daily KR YouTube trending snapshot -> GCS data lake.

Schedule ``30 15 * * *`` (UTC 15:30 == KST 00:30). Captures the finalized KR
trending list for the day and appends it as a ``dt=YYYY-MM-DD`` hive partition.

Required config (env var or Airflow Variable):
  * ``YOUTUBE_API_KEY``   - YouTube Data API v3 developer key
  * ``YOUTUBE_LAKE_BUCKET`` - GCS bucket name (no ``gs://`` prefix)

GCP auth: Application Default Credentials locally (``gcloud auth
application-default login``), Workload Identity in prod (K8s).
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

from autoresearch.youtube_collection.fetch import collect_trending
from autoresearch.youtube_collection.load import write_partition

logger = logging.getLogger(__name__)

LAKE_DIR_NAME = "data_lake/youtube_trending_kr"
DEFAULT_MAX_RESULTS = 200


def _get_config(name: str, default: str | None = None) -> str | None:
    """Read a setting from env first, then Airflow Variable."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        return Variable.get(name, default_var=default)
    except Exception:
        return default


def _build_service():
    """Build a YouTube Data API v3 service from the google-api-python-client."""
    from googleapiclient.discovery import build

    api_key = _get_config("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY is not set (env or Airflow Variable)")
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def _make_callables(service):
    """Adapt googleapiclient resources to the plain callables fetch.py expects."""
    return (
        lambda **kw: service.videos().list(**kw).execute(),
        lambda **kw: service.channels().list(**kw).execute(),
        lambda **kw: service.videoCategories().list(**kw).execute(),
    )


def _gcs_filesystem():
    """Default-cred GCS filesystem (ADC locally, Workload Identity in prod)."""
    import pyarrow.fs as fs

    return fs.GcsFileSystem()


@dag(
    dag_id="youtube_trending_kr_daily",
    schedule="30 15 * * *",
    start_date=datetime(2026, 6, 25),
    catchup=False,
    tags=["youtube", "collection", "kr"],
    default_args={"retries": 2},
)
def youtube_trending_kr_daily():
    @task
    def snapshot() -> str:
        list_videos, list_channels, list_categories = _make_callables(
            _build_service()
        )
        kst = _kst()
        collected_at = datetime.now(kst)

        videos = collect_trending(
            list_videos,
            list_channels,
            list_categories,
            collected_at=collected_at,
            region_code="KR",
            max_results=DEFAULT_MAX_RESULTS,
        )

        bucket = _get_config("YOUTUBE_LAKE_BUCKET")
        if not bucket:
            raise RuntimeError(
                "YOUTUBE_LAKE_BUCKET is not set (env or Airflow Variable)"
            )
        base_path = f"{bucket}/{LAKE_DIR_NAME}"
        path = write_partition(
            videos, base_path, collected_at.date(), filesystem=_gcs_filesystem()
        )
        logger.info("Daily snapshot complete: %d videos -> %s", len(videos), path)
        return path

    snapshot()


def _kst():
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Seoul")


youtube_trending_kr_daily()
