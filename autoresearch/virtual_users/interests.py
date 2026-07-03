"""Extract deterministic interest features from normalized persona text."""

from dataclasses import dataclass

from autoresearch.virtual_users.categories import DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
from autoresearch.virtual_users.schema import SourcePersona


KEYWORD_ALIASES: dict[str, tuple[str, ...]] = {
    "music": ("music", "song", "artist", "playlist"),
    "beauty": ("beauty", "makeup", "fashion", "style"),
    "study": ("study", "learning", "education", "learner"),
    "design": ("design", "presentation", "creative"),
    "sports": ("sports", "football", "baseball", "basketball"),
    "travel": ("travel", "trip", "cafe"),
    "cooking": ("cooking", "culinary", "recipe", "food"),
    "lifestyle": ("lifestyle", "family", "home", "daily"),
    "gaming": ("game", "gaming", "esports"),
    "technology": ("technology", "tech", "developer", "software"),
}


@dataclass(frozen=True)
class VirtualUserInterests:
    hobby_keywords: list[str]
    interest_keywords: list[str]
    category_affinity: dict[str, float]


CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Gaming": ("game", "gaming", "esports"),
    "Music": ("music", "song", "artist", "playlist"),
    "Entertainment": ("creator", "short-form", "video", "entertainment", "comedy"),
    "Education": ("study", "learning", "education", "learner"),
    "News & Politics": ("news", "politics", "current affairs"),
    "Sports": ("sports", "football", "baseball", "basketball"),
    "Science & Technology": ("technology", "tech", "developer", "software", "coding"),
    "Howto & Style": ("beauty", "makeup", "fashion", "style", "cooking", "recipe"),
    "People & Blogs": ("family", "home", "daily", "lifestyle", "travel", "cafe"),
    "Comedy": ("comedy", "funny", "humor"),
}


def _persona_text(persona: SourcePersona) -> str:
    """Join persona fields that are useful for deterministic keyword extraction."""

    parts = [
        persona.persona,
        persona.hobbies_and_interests,
        " ".join(persona.hobbies_and_interests_list),
        persona.professional_persona,
        persona.skills_and_expertise,
        persona.sports_persona,
        persona.arts_persona,
        persona.travel_persona,
        persona.culinary_persona,
        persona.family_persona,
        persona.cultural_background,
    ]
    return " ".join(part for part in parts if part).lower()


def extract_interest_keywords(persona: SourcePersona, limit: int = 10) -> list[str]:
    """Extract stable recommendation keywords from predefined aliases."""

    text = _persona_text(persona)
    keywords: list[str] = []
    for keyword, aliases in KEYWORD_ALIASES.items():
        if any(alias in text for alias in aliases):
            keywords.append(keyword)

    if not keywords:
        return ["general"]
    return keywords[:limit]


def _extract_hobby_keywords(persona: SourcePersona, limit: int = 10) -> list[str]:
    explicit = [
        part.strip().lower()
        for part in persona.hobbies_and_interests.split(",")
        if part.strip()
    ]
    explicit.extend(item.strip().lower() for item in persona.hobbies_and_interests_list)

    deduped: list[str] = []
    seen: set[str] = set()
    for keyword in explicit:
        if keyword not in seen:
            deduped.append(keyword)
            seen.add(keyword)

    if deduped:
        return deduped[:limit]
    return extract_interest_keywords(persona, limit=limit)


def _category_affinity(persona: SourcePersona) -> dict[str, float]:
    text = _persona_text(persona)
    scores: dict[str, float] = {}
    for category in DEFAULT_KAGGLE_YOUTUBE_CATEGORIES:
        aliases = CATEGORY_KEYWORDS.get(category, ())
        hits = sum(1 for alias in aliases if alias in text)
        if hits:
            scores[category] = min(0.95, 0.45 + hits * 0.15)

    if not scores:
        scores["Entertainment"] = 0.5
        scores["People & Blogs"] = 0.45
    return scores


def extract_virtual_user_interests(persona: SourcePersona) -> VirtualUserInterests:
    """Build deterministic fallback interest features for GLM output rows."""

    return VirtualUserInterests(
        hobby_keywords=_extract_hobby_keywords(persona),
        interest_keywords=extract_interest_keywords(persona),
        category_affinity=_category_affinity(persona),
    )
