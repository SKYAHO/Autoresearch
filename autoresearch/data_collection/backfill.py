"""과거 데이터 일괄 적재(backfill) 모듈.

Kaggle global parquet(전 세계 113개국, ~1.5GB)에서 한국(KR) 행만 걸러 정규화한 뒤,
각 행의 트렌딩 날짜별로 dt= 파티션을 만들어 GCS Data Lake 에 일괄 적재한다.

왜 pushdown 필터인가?
    KR 행이 ~12만 건 / ~21MB 수준이지만 원본은 전 세계 ~1천만 행(1.5GB)이다.
    읽기 단계에서 pyarrow filters 로 KR 행만 가져오면 메모리/시간을 KR 분량으로
    제한할 수 있다(전체 로드 후 마스크 불필요). 이후 to_pylist → group → write.

적재 구조:
    collected_at 은 이 backfill 실행 시각으로 통일(과거 행들의 collected_at 이
    같은 '지금'이 된다 — 어차피 freshness 신호이고, 과거 point-in-time 재구성은
    video_trending_date 로 하므로 무방). 각 행은 video_trending_date.date() 별로
    그룹핑되어 그날의 파티션에 들어간다.
"""
import logging
from collections import defaultdict
from datetime import UTC, date, datetime

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

    # 1) 원본 읽기 + 읽기 단계에서 KR pushdown 필터.
    #    전 세계 ~1천만 행을 메모리에 다 올리지 않고, 읽기에서 KR 행만 가져온다.
    kr_table = pq.read_table(
        source_path,
        filters=[("video_trending_country", "in", list(COUNTRY_ALIASES))],
    )
    kr_rows = kr_table.to_pylist()
    logger.info("Backfill: %d KR rows (pushdown-filtered)", len(kr_rows))

    # 2) 각 행 정규화 후 트렌딩 날짜별로 그룹핑.
    #    단일 행이 비정형이면 전체 배치(수만 행/수백 파티션)가 중단되지 않도록
    #    per-row 예외 처리: 실패 행은 로그 후 skip 하고 계속 진행.
    by_date: dict[date, list[TrendingVideo]] = defaultdict(list)
    skipped = 0
    for row in kr_rows:
        try:
            video = normalize_kaggle_row(row, collected_at=collected_at)
        except ValueError as exc:  # 정규화/검증(국가·datetime) 실패
            logger.warning("Backfill: skipping malformed row: %s", exc)
            skipped += 1
            continue
        by_date[video.video_trending_date.date()].append(video)
    if skipped:
        logger.warning("Backfill: skipped %d malformed rows", skipped)

    # 4) 날짜순으로 각 파티션 적재(정렬은 로깅/재현성을 위한 것, 결과는 동일).
    total = 0
    for partition_date, videos in sorted(by_date.items()):
        write_partition(videos, base_path, partition_date, filesystem=filesystem)
        total += len(videos)
    logger.info("Backfill: wrote %d rows across %d partitions", total, len(by_date))
    return total
