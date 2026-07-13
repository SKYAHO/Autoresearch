"""KR TrendingVideo 샘플을 로드해 정규 VideoRecord dict로 만든다.

VideoRecord 계약(키): video_id, title, description, tags(list[str]),
view_count, like_count, comment_count, channel_name, published_at(str).

원천(asaniczka)엔 카테고리·영상 길이 컬럼이 없다. 카테고리는 candidate 관련도에서
title/tags substring으로 대체하고, 영상 길이는 `nominal_duration_sec`로 결정론적
근사값을 만든다.
"""
import hashlib
import logging
from pathlib import Path

import pyarrow.parquet as pq


logger = logging.getLogger(__name__)

_MIN_DURATION = 60
_MAX_DURATION = 900


def nominal_duration_sec(video_id: str) -> int:
    """영상 길이 컬럼이 없는 데이터셋을 위해 video_id 기반 결정론적 근사 길이(초)."""

    digest = hashlib.sha256(video_id.encode("utf-8")).hexdigest()
    span = _MAX_DURATION - _MIN_DURATION
    return _MIN_DURATION + int(digest, 16) % (span + 1)


def _parse_tags(value: object) -> list[str]:
    """asaniczka video_tags(콤마 조인 문자열, 'None' 포함)를 tag 리스트로 파싱한다."""

    if isinstance(value, list):
        return [str(t).strip() for t in value if str(t).strip()]
    if not value:
        return []
    text = str(value).strip()
    if not text or text.lower() == "none":
        return []
    return [t.strip() for t in text.split(",") if t.strip() and t.strip().lower() != "none"]


def _int(value: object) -> int:
    """카운트류를 안전하게 int로. 실패 시 0."""

    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _first_present(row: dict, *keys: str) -> object:
    """row에서 값이 있는 첫 번째 key를 반환한다."""

    for key in keys:
        value = row.get(key)
        if value is not None:
            return value
    return None


def _to_video_record(row: dict) -> dict:
    """원천 parquet row 한 건을 정규 VideoRecord dict로 변환한다."""

    return {
        "video_id": str(row.get("video_id", "")),
        "title": str(_first_present(row, "title", "video_title") or ""),
        "description": str(
            _first_present(row, "description", "video_description") or ""
        ),
        "tags": _parse_tags(_first_present(row, "tags", "video_tags")),
        "view_count": _int(_first_present(row, "view_count", "video_view_count")),
        "like_count": _int(_first_present(row, "like_count", "video_like_count")),
        "comment_count": _int(
            _first_present(row, "comment_count", "video_comment_count")
        ),
        "channel_name": str(
            _first_present(row, "channel_name", "channel_title") or ""
        ),
        "published_at": str(
            _first_present(row, "publish_date", "video_published_at") or ""
        ),
    }


def load_video_records(path: str | Path, *, filesystem=None) -> list[dict]:
    """KR TrendingVideo parquet을 읽어 video_id로 dedup된 VideoRecord 목록을 반환한다."""

    table = pq.read_table(path, filesystem=filesystem)
    seen: set[str] = set()
    records: list[dict] = []
    for row in table.to_pylist():
        record = _to_video_record(row)
        vid = record["video_id"]
        if not vid or vid in seen:
            continue
        seen.add(vid)
        records.append(record)
    logger.info("Loaded video records", extra={"path": str(path), "count": len(records)})
    return records


def build_fixture_video_records(count: int = 40) -> list[dict]:
    """외부 데이터 없이 테스트할 수 있는 deterministic VideoRecord fixture."""

    themes = [
        ("게임 LCK 하이라이트", ["LCK", "롤", "게임"], "Gaming"),
        ("최신 K-POP 뮤직비디오", ["KPOP", "뮤직", "아이돌"], "Music"),
        ("초간단 집밥 레시피", ["요리", "레시피", "먹방"], "Food"),
        ("해외여행 브이로그", ["여행", "브이로그", "vlog"], "Travel"),
        ("파이썬 코딩 강의", ["코딩", "개발", "파이썬"], "Education"),
    ]
    records: list[dict] = []
    for index in range(count):
        title, tags, topic = themes[index % len(themes)]
        vid = f"vid_{index:04d}"
        records.append(
            {
                "video_id": vid,
                "title": f"{title} #{index}",
                "description": f"{topic} 관련 영상 {index}. {title}.",
                "tags": tags,
                "view_count": 100000 + index * 1000,
                "like_count": 5000 + index * 50,
                "comment_count": 200 + index,
                "channel_name": f"channel_{index % 7}",
                "published_at": "2025-06-01T00:00:00+00:00",
            }
        )
    return records
