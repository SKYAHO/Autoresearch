"""[일일 수집 DAG] 한국(KR) 유튜브 트렌딩 → GCS Data Lake.

매일 그날의 KR 트렌딩 영상(약 200개)을 수집해 ``dt=YYYY-MM-DD`` hive 파티션으로
GCS 레이크에 append 한다. append-only 일별 스냅샷 모델의 실시간 축.

스케줄: ``30 15 * * *`` (UTC 15:30 == KST 00:30)
    → 그날 트렌딩이 어느 정도 확정된 새벽에 찍는다. dt 키는 KST 수집일.
    (Kaggle 의 video_trending_date 도 같은 의미라 과거/실시간 데이터가 일관됨.)

필요 설정(환경변수 또는 Airflow Variable):
  * ``YOUTUBE_API_KEY``    - YouTube Data API v3 개발자 키
  * ``YOUTUBE_LAKE_BUCKET`` - GCS 버킷명(gs:// 접두사 없음)

인증:
  * GCP: Application Default Credentials(로컬은 ``gcloud auth application-default
    login``, prod K8s 은 Workload Identity — 최현규 인프라 담당).
  * YouTube: API 키만으로 충분(쿼터 ~7 units/일, 예산 10,000 대비 0.07%).

이 파일은 '얇은 래퍼'다. 실제 로직은 autoresearch.youtube_collection(순수 Python,
단위테스트 가능)에 있고, 여기선 Airflow TaskFlow 로 그것들을 엮기만 한다.
"""
from __future__ import annotations

import logging
import os
import sys
from datetime import datetime
from pathlib import Path

# DAG 파일은 dags/ 아래에 있지만 autoresearch 패키지는 레포 루트에 있다.
# 레포 루트를 import 경로에 추가해서 autoresearch.* 를 불러올 수 있게 한다.
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from airflow.decorators import dag, task
from airflow.models import Variable

from autoresearch.youtube_collection.fetch import collect_trending
from autoresearch.youtube_collection.load import write_partition

logger = logging.getLogger(__name__)

# 레이크 내 디렉터리. base_path = {bucket}/data_lake/youtube_trending_kr
LAKE_DIR_NAME = "data_lake/youtube_trending_kr"
# KR 하루 트렌딩 규모. 약 200개.
DEFAULT_MAX_RESULTS = 200


def _get_config(name: str, default: str | None = None) -> str | None:
    """설정값 읽기: 환경변수 우선, 없으면 Airflow Variable."""
    value = os.environ.get(name)
    if value:
        return value
    try:
        return Variable.get(name, default_var=default)
    except Exception:
        return default


def _build_service():
    """google-api-python-client 으로 YouTube Data API v3 서비스 객체 생성."""
    from googleapiclient.discovery import build

    api_key = _get_config("YOUTUBE_API_KEY")
    if not api_key:
        raise RuntimeError("YOUTUBE_API_KEY is not set (env or Airflow Variable)")
    return build("youtube", "v3", developerKey=api_key, cache_discovery=False)


def _make_callables(service):
    """googleapiclient resource → fetch.py 가 기대하는 평범한 callable 로 adapt.

    fetch.py 는 googleapiclient 를 몰라야(테스트 용이) 하므로, 여기서
    ``service.videos().list(**kw).execute()`` 모양을 ``list_videos(**kw)`` 로 감싼다.
    """
    return (
        lambda **kw: service.videos().list(**kw).execute(),
        lambda **kw: service.channels().list(**kw).execute(),
        lambda **kw: service.videoCategories().list(**kw).execute(),
    )


def _gcs_filesystem():
    """기본 인증(ADC) 기반 GCS 파일시스템. load.py 의 filesystem 인자로 전달."""
    import pyarrow.fs as fs

    return fs.GcsFileSystem()


@dag(
    dag_id="youtube_trending_kr_daily",
    schedule="30 15 * * *",  # UTC 15:30 = KST 00:30
    start_date=datetime(2026, 6, 25),
    catchup=False,  # 과거 분할 실행(백필) 안 함 — 백필은 별도 DAG.
    tags=["youtube", "collection", "kr"],
    default_args={"retries": 2},  # 일시적 API 실패 대비 재시도 2회.
)
def youtube_trending_kr_daily():
    @task
    def snapshot() -> str:
        # 1) API 서비스 빌드 + callable adapt
        list_videos, list_channels, list_categories = _make_callables(
            _build_service()
        )
        # 2) 수집 시각 = 현재 KST. 이것이 dt 키와 video_trending_date 가 됨.
        kst = _kst()
        collected_at = datetime.now(kst)

        # 3) 트렌딩 수집 + 채널 메타 + 카테고리 변환 + 정규화
        videos = collect_trending(
            list_videos,
            list_channels,
            list_categories,
            collected_at=collected_at,
            region_code="KR",
            max_results=DEFAULT_MAX_RESULTS,
        )

        # 4) GCS 파티션 적재. base_path 는 bucket-상대경로(gs:// 없음).
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
    """Asia/Seoul 타임존 객체."""
    from zoneinfo import ZoneInfo

    return ZoneInfo("Asia/Seoul")


youtube_trending_kr_daily()
