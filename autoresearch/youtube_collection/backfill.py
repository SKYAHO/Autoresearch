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
    """One-time historical load: global Kaggle parquet -> KR-only dt partitions.

    Reads the full source table, keeps only KR rows, normalizes each, and writes
    one partition per distinct video_trending_date. Returns total rows written.
    """
    if collected_at is None:
        collected_at = datetime.now(UTC)

    table = pq.read_table(source_path)
    kr_mask = pc.is_in(
        table["video_trending_country"],
        value_set=pa.array(list(COUNTRY_ALIASES)),
    )
    kr_rows = table.filter(kr_mask).to_pylist()
    logger.info(
        "Backfill: %d KR rows out of %d total", len(kr_rows), table.num_rows
    )

    by_date: dict[date, list[TrendingVideo]] = defaultdict(list)
    for row in kr_rows:
        video = normalize_kaggle_row(row, collected_at=collected_at)
        by_date[video.video_trending_date.date()].append(video)

    total = 0
    for partition_date, videos in sorted(by_date.items()):
        write_partition(videos, base_path, partition_date, filesystem=filesystem)
        total += len(videos)
    logger.info("Backfill: wrote %d rows across %d partitions", total, len(by_date))
    return total
