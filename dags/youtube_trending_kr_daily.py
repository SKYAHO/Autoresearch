"""[일일 수집 DAG] 한국(KR) 유튜브 트렌딩 → GCS Data Lake.

매일 그날의 KR 트렌딩 영상(약 200개)을 수집해 ``dt=YYYY-MM-DD`` hive 파티션으로
GCS 레이크에 append 한다. append-only 일별 스냅샷 모델의 실시간 축.

스케줄: ``30 15 * * *`` (UTC 15:30 == KST 00:30)
    → 그날 트렌딩이 어느 정도 확정된 새벽에 찍는다. dt 키는 KST 수집일.

필요 설정(환경변수 또는 Airflow Variable):
  * ``YOUTUBE_API_KEYS``  - YouTube Data API v3 키(쉼표 구분 복수, Key 무효화 대응)
  * ``YOUTUBE_LAKE_BUCKET`` - GCS 버킷명(gs:// 접두사 없음)
  * ``YOUTUBE_PROXY_URL``  - (선택) Cloud Run 프록시 URL. 1차: 미설정=비활성.

인증:
  * GCP: Application Default Credentials(로컬은 ``gcloud auth application-default
    login``, prod K8s 은 Workload Identity — 인프라 담당).
  * YouTube: API 키 풀만으로 충분(쿼터 ~7 units/일, 예산 10,000 대비 0.07%).

복원력:
  * ResilientYouTubeClient 가 재시도(tenacity) + Key 롤링 + IP밴 시그니처 +
    Circuit Breaker + skip 을 담당. CollectionExhausted → AirflowFailException
    승격으로 터미널 실패(retries 무관 즉시 failed).

이 파일은 '얇은 래퍼'다. 실제 로직은 autoresearch.youtube_collection(순수 Python,
단위테스트 가능)에 있고, 여기선 Airflow TaskFlow 로 그것들을 엮기만 한다.
"""

from __future__ import annotations

import logging
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from zoneinfo import ZoneInfo

# DAG 파일은 dags/ 아래에 있지만 autoresearch 패키지는 레포 루트에 있다.
# 레포 루트를 import 경로에 추가해서 autoresearch.* 를 불러올 수 있게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airflow.decorators import dag, task
from airflow.exceptions import AirflowFailException
from airflow.models import Variable

from autoresearch.youtube_collection.client import (
    CollectionExhausted,
    ResilientYouTubeClient,
    YouTubeCallables,
)
from autoresearch.youtube_collection.fetch import collect_trending
from autoresearch.youtube_collection.load import write_partition

logger = logging.getLogger(__name__)

# 레이크 내 디렉터리. base_path = {bucket}/data_lake/youtube_trending_kr
LAKE_DIR_NAME = "data_lake/youtube_trending_kr"
# KR 하루 트렌딩 규모. 약 200개.
DEFAULT_MAX_RESULTS = 200
# dt 키는 KST 일자 기준(KR 트렌딩 날). collected_at 은 UTC로 저장(백필과 통일).
_KST = ZoneInfo("Asia/Seoul")


def _get_config(name: str, default: str | None = None) -> str | None:
    """설정값 읽기: 환경변수 우선, 없으면 Airflow Variable."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        return Variable.get(name, default_var=default)
    except Exception:
        return default


def _load_keys() -> list[str]:
    """API Key 풀 로드: YOUTUBE_API_KEYS(복수, 쉼표) 우선, 없으면 단수 폴백."""
    raw = _get_config("YOUTUBE_API_KEYS")
    if raw:
        keys = [k.strip() for k in raw.split(",") if k.strip()]
        if keys:
            return keys
    single = _get_config("YOUTUBE_API_KEY")
    if single:
        return [single]
    raise RuntimeError("YOUTUBE_API_KEYS(또는 YOUTUBE_API_KEY) 가 설정되지 않음")


def _build_client() -> ResilientYouTubeClient:
    """ResilientYouTubeClient 생성. proxy_url 은 환경변수(1차: 보통 None)."""
    return ResilientYouTubeClient(
        keys=_load_keys(),
        proxy_url=_get_config("YOUTUBE_PROXY_URL"),
    )


def _gcs_filesystem():
    """기본 인증(ADC) 기반 GCS 파일시스템. load.py 의 filesystem 인자로 전달."""
    import pyarrow.fs as fs

    return fs.GcsFileSystem()


@dag(
    dag_id="youtube_trending_kr_daily",
    schedule="30 15 * * *",  # UTC 15:30 = KST 00:30
    start_date=datetime(2026, 6, 25, tzinfo=_KST),  # tz-aware 권장
    catchup=False,  # 과거 분할 실행(백필) 안 함 — 백필은 별도 DAG.
    tags=["youtube", "collection", "kr"],
    default_args={"retries": 2},  # 일시적 API 실패 대비 재시도 2회.
)
def youtube_trending_kr_daily():
    @task
    def snapshot() -> str:
        client = _build_client()
        callables: YouTubeCallables = client.make_callables()
        collected_at = datetime.now(UTC)
        partition_date = collected_at.astimezone(_KST).date()

        try:
            videos = collect_trending(
                callables.list_videos,
                callables.list_channels,
                callables.list_categories,
                collected_at=collected_at,
                region_code="KR",
                max_results=DEFAULT_MAX_RESULTS,
            )
        except CollectionExhausted as e:
            # 터미널: 모든 Key·경로 소진. 재시도 무의미 → 즉시 failed 승격.
            raise AirflowFailException(
                f"유튜브 수집 폭주 — 그날 partition skip: {e}"
            ) from e

        bucket = _get_config("YOUTUBE_LAKE_BUCKET")
        if not bucket:
            raise RuntimeError(
                "YOUTUBE_LAKE_BUCKET is not set (env or Airflow Variable)"
            )
        base_path = f"{bucket}/{LAKE_DIR_NAME}"
        path = write_partition(
            videos, base_path, partition_date, filesystem=_gcs_filesystem()
        )
        logger.info("Daily snapshot complete: %d videos -> %s", len(videos), path)
        return path

    snapshot()


youtube_trending_kr_daily()
