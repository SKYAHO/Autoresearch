"""[일일 action log DAG] YouTube daily partition + virtual users → GCS event log.

매일 KR YouTube trending 200개 partition이 GCS에 적재된 뒤, 같은 날짜의
virtual user action log를 생성해 ``data_lake/action_log/dt=YYYY-MM-DD``에 쓴다.

스케줄: ``0 16 * * *`` (UTC 16:00 == KST 01:00)
    → ``youtube_trending_kr_daily``(KST 00:30) 이후 실행한다.

필요 설정(환경변수 또는 Airflow Variable):
  * ``YOUTUBE_LAKE_BUCKET`` - GCS 버킷명(gs:// 접두사 없음)

선택 설정:
  * ``ACTION_LOG_VIRTUAL_USERS_PATH`` - virtual user parquet 경로
  * ``ACTION_LOG_OUTPUT_DIR`` - action log 출력 루트
  * ``ACTION_LOG_QUARANTINE_DIR`` - quarantine 출력 루트
  * ``ACTION_LOG_GENERATOR`` - rule_based 또는 openrouter
  * ``ACTION_LOG_CANDIDATES_PER_USER`` - 기본 24
  * ``ACTION_LOG_TARGET_CTR`` - 기본 0.02
  * ``ACTION_LOG_MAX_CONCURRENCY`` - 기본 1
  * ``ACTION_LOG_CHUNK_SIZE`` - 기본 0(청킹 없음)
"""
from __future__ import annotations

import logging
import os
from datetime import UTC, date, datetime, timedelta
from zoneinfo import ZoneInfo

from airflow.decorators import dag, task
from airflow.models import Variable

from autoresearch.action_logs.daily import run_daily_action_log


logger = logging.getLogger(__name__)

_KST = ZoneInfo("Asia/Seoul")
YOUTUBE_LAKE_DIR = "data_lake/youtube_trending_kr"
ACTION_LOG_LAKE_DIR = "data_lake/action_log"
ACTION_LOG_QUARANTINE_DIR = "data_lake/action_log_quarantine"
DEFAULT_VIRTUAL_USERS_PATH = "asset/virtual_user/vu_1000.parquet"


def _get_config(name: str, default: str | None = None) -> str | None:
    """설정값 읽기: 환경변수 우선, 없으면 Airflow Variable."""

    value = os.environ.get(name)
    if value:
        return value
    try:
        value = Variable.get(name, default_var=None)
        return value if value else default
    except Exception:
        return default


def _get_int_config(name: str, default: int) -> int:
    """정수 설정값을 읽는다."""

    value = _get_config(name)
    return int(value) if value else default


def _get_float_config(name: str, default: float) -> float:
    """실수 설정값을 읽는다."""

    value = _get_config(name)
    return float(value) if value else default


def _gcs_filesystem():
    """기본 인증(ADC) 기반 GCS 파일시스템."""

    import pyarrow.fs as fs

    return fs.GcsFileSystem()


def _partition_date_from_context(context) -> date:
    """수동 param이 있으면 그 날짜, 없으면 현재 KST 날짜를 사용한다."""

    params = context.get("params") or {}
    value = params.get("partition_date") or _get_config("ACTION_LOG_PARTITION_DATE")
    if value:
        return date.fromisoformat(str(value))
    return datetime.now(UTC).astimezone(_KST).date()


@dag(
    dag_id="youtube_action_log_daily",
    schedule="0 16 * * *",  # UTC 16:00 = KST 01:00
    start_date=datetime(2026, 7, 1, tzinfo=_KST),
    catchup=False,
    tags=["youtube", "action-log", "kr"],
    default_args={"retries": 3, "retry_delay": timedelta(minutes=10)},
    params={"partition_date": ""},
)
def youtube_action_log_daily():
    @task
    def generate(**context) -> dict[str, object]:
        bucket = _get_config("YOUTUBE_LAKE_BUCKET")
        if not bucket:
            raise RuntimeError("YOUTUBE_LAKE_BUCKET is not set (env or Airflow Variable)")

        partition_date = _partition_date_from_context(context)
        youtube_base_path = _get_config(
            "ACTION_LOG_YOUTUBE_BASE_PATH",
            f"{bucket}/{YOUTUBE_LAKE_DIR}",
        )
        virtual_users_path = _get_config(
            "ACTION_LOG_VIRTUAL_USERS_PATH",
            f"{bucket}/{DEFAULT_VIRTUAL_USERS_PATH}",
        )
        output_base_path = _get_config(
            "ACTION_LOG_OUTPUT_DIR",
            f"{bucket}/{ACTION_LOG_LAKE_DIR}",
        )
        quarantine_base_path = _get_config(
            "ACTION_LOG_QUARANTINE_DIR",
            f"{bucket}/{ACTION_LOG_QUARANTINE_DIR}",
        )

        summary = run_daily_action_log(
            partition_date=partition_date,
            youtube_base_path=youtube_base_path,
            virtual_users_path=virtual_users_path,
            output_base_path=output_base_path,
            quarantine_base_path=quarantine_base_path,
            filesystem=_gcs_filesystem(),
            candidates_per_user=_get_int_config("ACTION_LOG_CANDIDATES_PER_USER", 24),
            target_ctr=_get_float_config("ACTION_LOG_TARGET_CTR", 0.02),
            personalized_ratio=_get_float_config("ACTION_LOG_PERSONALIZED_RATIO", 0.7),
            popular_ratio=_get_float_config("ACTION_LOG_POPULAR_RATIO", 0.2),
            exploration_ratio=_get_float_config("ACTION_LOG_EXPLORATION_RATIO", 0.1),
            seed=_get_int_config("ACTION_LOG_SEED", 42),
            max_concurrency=_get_int_config("ACTION_LOG_MAX_CONCURRENCY", 1),
            chunk_size=_get_int_config("ACTION_LOG_CHUNK_SIZE", 0),
            generator_name=_get_config("ACTION_LOG_GENERATOR", "rule_based")
            or "rule_based",
            model_name=_get_config("ACTION_LOG_MODEL_NAME"),
        )
        logger.info("Daily action log complete", extra=summary)
        return summary

    generate()


youtube_action_log_daily()
