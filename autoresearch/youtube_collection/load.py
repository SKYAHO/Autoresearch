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
    """Write a daily snapshot as a single snappy parquet file under a dt= hive partition.

    Overwrites part-0.parquet so re-runs for the same date are idempotent.
    Pass a pyarrow filesystem (e.g. GcsFileSystem) to target GCS; default is local.
    """
    file_path = f"{base_path}/dt={partition_date:%Y-%m-%d}/{PARTITION_FILE}"
    if filesystem is None:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
    table = _to_table(videos)
    pq.write_table(table, file_path, compression="snappy", filesystem=filesystem)
    logger.info("Wrote %d rows to %s", table.num_rows, file_path)
    return file_path


def _to_table(videos: list[TrendingVideo]):
    records = [v.model_dump() for v in videos]
    return pa.Table.from_pylist(records)
