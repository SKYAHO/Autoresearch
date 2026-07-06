"""[백필 DAG] Kaggle global parquet → KR dt 파티션 일괄 적재(1회성).

과거 데이터(2024-10-12 ~ 2026-06-30, KR 12만여 행 / 620일)를 한 번에 GCS 레이크에
채운다. 일일 DAG 와 달리 스케줄 없음(schedule=None) — 수동 트리거 전용.

트리거 시 Params 로 경로 덮어쓰기 가능:
  * ``source_path`` - 원본 global parquet(``gs://...`` 또는 로컬 경로)
  * ``base_path``   - 적재 루트, bucket-상대경로(예: ``bucket/data_lake/...``)

설정 안 하면 환경변수/Airflow Variable 로 폴백:
  ``YOUTUBE_BACKFILL_SOURCE``, ``YOUTUBE_LAKE_BUCKET``

주의: 백필은 멱등하지만 '전체 재적재' 성격이라, 기존 데이터를 갱신하려면
파티션을 지우고 다시 돌리는 게 안전하다(write_partition 은 dt 단위 덮어쓰지만
과거에 있던 날짜가 새 파일에 없으면 잔류).
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# 레포 루트를 import 경로에 추가(dags/ 안에서 autoresearch.* 사용).
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airflow.decorators import dag, task
from airflow.models import Variable

from autoresearch.youtube_collection.backfill import backfill_from_parquet

logger = logging.getLogger(__name__)

LAKE_DIR_NAME = "data_lake/youtube_trending_kr"


def _get_config(name: str, default: str | None = None) -> str | None:
    """설정값 읽기: 환경변수 우선, 없으면 Airflow Variable."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        return Variable.get(name, default_var=default)
    except Exception:
        return default


def _gcs_filesystem():
    """기본 인증(ADC) 기반 GCS 파일시스템. 적재(write)용."""
    import pyarrow.fs as fs

    return fs.GcsFileSystem()


@dag(
    dag_id="youtube_backfill_kr",
    schedule=None,  # 수동 트리거만.
    start_date=datetime(2026, 6, 25, tzinfo=ZoneInfo("Asia/Seoul")),  # tz-aware
    catchup=False,
    tags=["youtube", "collection", "kr", "backfill"],
    # 트리거 시점에 UI/CLI 에서 덮어쓸 수 있는 파라미터.
    params={
        "source_path": "",
        "base_path": "",
    },
)
def youtube_backfill_kr():
    @task
    def run(**context) -> int:
        # 1) params 우선, 없으면 Variable/env 폴백.
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

        # 2) 일괄 적재. 읽기는 pq 가 gs:// 를 자동 처리, 쓰기는 filesystem(GCS).
        total = backfill_from_parquet(source_path, base_path, filesystem=_gcs_filesystem())
        logger.info("Backfill complete: %d rows -> %s", total, base_path)
        return total

    run()


youtube_backfill_kr()
