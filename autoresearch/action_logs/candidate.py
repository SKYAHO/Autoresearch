"""유저별 노출(candidate) batch 구성 — Z 하이브리드(관련 + exploration).

카테고리 컬럼 없이도 동작하도록, 관련도는 유저 관심 키워드가 영상 텍스트
(title/tags/description)에 등장하는지의 substring 겹침으로 계산한다(한국어 대응).
클릭 판단 자체는 LLM이 실제 title/description을 읽어 수행한다.
"""
import logging
import random

from autoresearch.action_logs.schema import (
    EXPOSURE_EXPLORATION,
    EXPOSURE_TOP_RANKED,
)


logger = logging.getLogger(__name__)


def _user_keywords(virtual_user: dict) -> list[str]:
    """VirtualUser에서 관련도 계산에 쓸 관심 키워드를 모은다."""

    keys = (
        "primary_categories",
        "interest_keywords",
        "hobby_keywords",
        "lifestyle_keywords",
    )
    words: list[str] = []
    for key in keys:
        value = virtual_user.get(key) or []
        if isinstance(value, list):
            words.extend(str(v) for v in value if v)
    # category_affinity 키(카테고리명)도 관심 신호.
    affinity = virtual_user.get("category_affinity") or {}
    if isinstance(affinity, dict):
        words.extend(str(k) for k in affinity)
    return [w.strip().lower() for w in words if w and w.strip()]


def _video_text(video: dict) -> str:
    """관련도 매칭에 쓸 영상 텍스트(title + tags + description)."""

    tags = video.get("tags") or []
    tag_text = " ".join(str(t) for t in tags) if isinstance(tags, list) else str(tags)
    return " ".join(
        [str(video.get("title", "")), tag_text, str(video.get("description", ""))]
    ).lower()


def _relevance_score(keywords: list[str], video_text: str) -> int:
    """유저 키워드가 영상 텍스트에 등장하는 개수(substring 겹침)."""

    return sum(1 for kw in keywords if kw and kw in video_text)


def build_candidates(
    virtual_user: dict,
    videos: list[dict],
    candidates_per_user: int,
    exploration_ratio: float,
    rng: random.Random,
) -> list[tuple[dict, str]]:
    """유저 1명의 노출 batch를 (video, exposure_type) 목록으로 구성한다.

    관련 후보(top_ranked) + exploration 랜덤. pool이 요청 수보다 작으면 가능한 만큼만.
    """
    if not videos:
        return []

    n_total = min(candidates_per_user, len(videos))
    n_explore = min(round(n_total * exploration_ratio), n_total)
    n_relevant = n_total - n_explore

    keywords = _user_keywords(virtual_user)
    scored = [
        (
            _relevance_score(keywords, _video_text(v)),
            int(v.get("view_count", 0) or 0),
            idx,
            v,
        )
        for idx, v in enumerate(videos)
    ]
    # 관련도 desc, 동점은 조회수 desc, 그다음 idx로 안정 정렬.
    scored.sort(key=lambda t: (-t[0], -t[1], t[2]))

    relevant = [t[3] for t in scored[:n_relevant]]
    remaining = [t[3] for t in scored[n_relevant:]]
    rng.shuffle(remaining)
    exploration = remaining[:n_explore]

    candidates = [(v, EXPOSURE_TOP_RANKED) for v in relevant]
    candidates += [(v, EXPOSURE_EXPLORATION) for v in exploration]
    rng.shuffle(candidates)

    logger.debug(
        "Built candidates",
        extra={
            "user_id": virtual_user.get("user_id"),
            "n_relevant": len(relevant),
            "n_exploration": len(exploration),
        },
    )
    return candidates
