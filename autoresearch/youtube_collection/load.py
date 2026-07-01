"""Data Lake(GCS Parquet) 적재 모듈.

append-only 일별 스냅샷 모델을 그대로 반영하는 파티션 적재 로직.

파티션 레이아웃(hive 단일키):
    {base_path}/dt=YYYY-MM-DD/part-0.parquet
      * dt 키는 하이픈 날짜(슬래시 yyyy/mm/dd 는 경로 구분자가 돼 hive 자동 감지가
        깨지므로 안티패턴).
      * dt 는 '파티션 컬럼'이라 파일 안에 컬럼으로 저장되지 않지만, hive-aware 로
        읽으면(pq.read_table(..., partitioning='hive')) dt 가 자동으로 파생된다.

멱등(idempotent): 같은 dt 를 다시 쓰면 part-0.parquet 을 덮어쓴다. 하루에 여러 번
수집해도 안전(마지막 스냅샷이 남음). 재실행/백필 안전성의 핵심.

filesystem 옵션:
    None(기본) → 로컬 파일 시스템(os.makedirs 로 디렉터리 생성).
    pyarrow.fs.GcsFileSystem() → GCS 에 bucket-상대경로(base_path 에 gs:// 없이)로 쓴다.
    이 분리 덕분에 테스트는 로컬(tmp_path)로, 프로덕션은 GCS 로 같은 코드를 쓴다.
"""
import logging
import os
from datetime import date

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.youtube_collection.schema import TrendingVideo


logger = logging.getLogger(__name__)

PARTITION_FILE = "part-0.parquet"


def write_partition(
    videos: list[TrendingVideo],
    base_path: str,
    partition_date: date,
    *,
    filesystem=None,
) -> str:
    """하루 분량 스냅샷을 단일 snappy parquet 1개로 dt= 파티션에 쓴다.

    Args:
        videos: 그날의 TrendingVideo 리스트(보통 KR 트렌딩 ~200개).
        base_path: 레이크 루트. GCS 면 bucket/data_lake/youtube_trending_kr
                   (gs:// 없음). 로컬이면 /tmp/.../lake 같은 경로.
        partition_date: dt=YYYY-MM-DD 의 날짜. 보통 collected_at.date().
        filesystem: None(로컬) 또는 pyarrow 파일시스템(GcsFileSystem 등).

    Returns:
        써진 파일 경로(로그/검증용).
    """
    # 로컬일 때만 디렉터리 보장 생성. GCS 는 객체 스토어라 디렉터리 개념이 없음.
    file_path = f"{base_path}/dt={partition_date:%Y-%m-%d}/{PARTITION_FILE}"
    if filesystem is None:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
    table = _to_table(videos)
    # snappy 압축(빠르고 parquet 표준). filesystem 이 GcsFileSystem 이면 GCS 로 업로드.
    pq.write_table(table, file_path, compression="snappy", filesystem=filesystem)
    logger.info("Wrote %d rows to %s", table.num_rows, file_path)
    return file_path


def _to_table(videos: list[TrendingVideo]):
    """TrendingVideo 리스트 → pyarrow Table.
    model_dump() 가 타입에 맞는 Python dict 를 주고, from_pylist 가 타입을 유지.
    """
    records = [v.model_dump() for v in videos]
    return pa.Table.from_pylist(records)
