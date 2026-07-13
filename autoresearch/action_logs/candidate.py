"""유저별 노출(candidate) batch 구성 — Z 하이브리드(관련 + exploration).

카테고리 컬럼 없이도 동작하도록, 관련도는 유저 관심 키워드가 영상 텍스트
(title/tags/description)에 등장하는지의 substring 겹침으로 계산한다(한국어 대응).
클릭 판단 자체는 LLM이 실제 title/description을 읽어 수행한다.
"""
import logging
import random


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
    *,
    personalized_ratio: float = 0.7,
    popular_ratio: float = 0.2,
) -> list[dict]:
    """유저 1명의 노출 batch를 video dict 목록으로 구성한다.

    personalized relevance + popular/trending + exploration 랜덤을 섞되,
    exposure_type 라벨은 로그에 남기지 않으므로 반환하지 않는다. pool이 요청 수보다
    작으면 가능한 만큼만.
    """
    if not videos:
        return []

    n_total = min(candidates_per_user, len(videos))
    ratio_sum = personalized_ratio + popular_ratio + exploration_ratio
    if ratio_sum <= 0:
        personalized_ratio, popular_ratio, exploration_ratio = 1.0, 0.0, 0.0
        ratio_sum = 1.0
    n_popular = min(round(n_total * popular_ratio / ratio_sum), n_total)
    n_explore = min(
        round(n_total * exploration_ratio / ratio_sum),
        n_total - n_popular,
    )
    n_relevant = n_total - n_popular - n_explore

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

    selected: list[dict] = []
    seen: set[str] = set()

    def add_unique(items: list[dict], limit: int) -> list[dict]:
        added: list[dict] = []
        for item in items:
            if len(added) >= limit:
                break
            video_id = str(item.get("video_id", ""))
            if not video_id or video_id in seen:
                continue
            seen.add(video_id)
            selected.append(item)
            added.append(item)
        return added

    relevant = add_unique([t[3] for t in scored], n_relevant)
    popular_pool = sorted(
        videos,
        key=lambda v: (-int(v.get("view_count", 0) or 0), str(v.get("video_id", ""))),
    )
    # popular_pool 전체를 스캔하므로 상위 인기 영상이 personalized와 겹쳐도
    # 다음 인기 영상으로 popular 슬롯을 채운다. 아래 fallback은 주로 blank/duplicate
    # video_id처럼 유효 unique pool이 n_total보다 작을 때만 의미가 있다.
    popular = add_unique(popular_pool, n_popular)
    remaining = [v for v in videos if str(v.get("video_id", "")) not in seen]
    rng.shuffle(remaining)
    exploration = add_unique(remaining, n_explore)

    if len(selected) < n_total:
        add_unique([t[3] for t in scored], n_total - len(selected))

    candidates = selected
    rng.shuffle(candidates)

    logger.debug(
        "Built candidates",
        extra={
            "user_id": virtual_user.get("user_id"),
            "n_relevant": len(relevant),
            "n_popular": len(popular),
            "n_exploration": len(exploration),
        },
    )
    return candidates
