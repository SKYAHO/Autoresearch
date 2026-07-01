"""과거 데이터 일괄 적재(backfill) 모듈.

Kaggle global parquet(전 세계 113개국, ~1.5GB)에서 한국(KR) 행만 걸러 정규화한 뒤,
각 행의 트렌딩 날짜별로 dt= 파티션을 만들어 GCS Data Lake 에 일괄 적재한다.

왜 한 번에 다 읽는가(스트리밍 X)?
    KR 행이 ~12만 건 / ~21MB 수준이라 pandas/pyarrow 가 메모리에 다 담는다.
    Spark 같은 분산 처리는 오버킬. 단순 read → filter → group → write.

왜 KR 필터를 pyarrow compute 로?
    to_pylist() 로 전체를 Python 객체로 바꾼 뒤 필터하면 1,000만 행을 다 변환해야
    느리다. pyarrow 의 벡터 연산(is_in)으로 KR 마스크를 먼저 만들어 12만 행만
    Python 으로 내리는 게 훨씬 빠르다.

적재 구조:
    collected_at 은 이 backfill 실행 시각으로 통일(과거 행들의 collected_at 이
    같은 '지금'이 된다 — 어차피 freshness 신호이고, 과거 point-in-time 재구성은
    video_trending_date 로 하므로 무방). 각 행은 video_trending_date.date() 별로
    그룹핑되어 그날의 파티션에 들어간다.
"""
import logging
from collections import defaultdict
from datetime import UTC, date, datetime

import pyarrow as pa
import pyarrow.compute as pc
import pyarrow.parquet as pq

from autoresearch.youtube_collection.load import write_partition
from autoresearch.youtube_collection.schema import TrendingVideo
from autoresearch.youtube_collection.transform import (
    COUNTRY_ALIASES,
    normalize_kaggle_row,
)


logger = logging.getLogger(__name__)


def backfill_from_parquet(
    source_path: str,
    base_path: str,
    *,
    collected_at: datetime | None = None,
    filesystem=None,
) -> int:
    """Kaggle global parquet → KR-only dt 파티션 일괄 적재. 쓰인 총 행 수 반환.

    Args:
        source_path: 원본 global parquet 경로(로컬 또는 gs:// URL).
        base_path: 적재 루트. GCS 면 bucket/data_lake/youtube_trending_kr.
        collected_at: 수집 시각. None 이면 실행 시각(now, UTC).
        filesystem: GCS 적재 시 GcsFileSystem 전달. 읽기는 pq 가 gs:// 자동 처리.
    """
    if collected_at is None:
        collected_at = datetime.now(UTC)

    # 1) 원본 전체 읽기
    table = pq.read_table(source_path)
    # 2) KR 행만 벡터 필터(COUNTRY_ALIASES 키 = {"KR","South Korea"}).
    kr_mask = pc.is_in(
        table["video_trending_country"],
        value_set=pa.array(list(COUNTRY_ALIASES)),
    )
    kr_rows = table.filter(kr_mask).to_pylist()
    logger.info(
        "Backfill: %d KR rows out of %d total", len(kr_rows), table.num_rows
    )

    # 3) 각 행 정규화 후 트렌딩 날짜별로 그룹핑.
    by_date: dict[date, list[TrendingVideo]] = defaultdict(list)
    for row in kr_rows:
        video = normalize_kaggle_row(row, collected_at=collected_at)
        by_date[video.video_trending_date.date()].append(video)

    # 4) 날짜순으로 각 파티션 적재(정렬은 로깅/재현성을 위한 것, 결과는 동일).
    total = 0
    for partition_date, videos in sorted(by_date.items()):
        write_partition(videos, base_path, partition_date, filesystem=filesystem)
        total += len(videos)
    logger.info("Backfill: wrote %d rows across %d partitions", total, len(by_date))
    return total
