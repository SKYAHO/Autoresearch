"""Persona 텍스트에서 추천 후보 매칭에 쓸 관심 keyword를 추출한다."""

import re

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


def _persona_text(persona: SourcePersona) -> str:
    """관심사 추출 대상이 되는 persona 텍스트 필드를 하나의 검색 문자열로 합친다."""

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


def _keyword_score(text: str, aliases: tuple[str, ...]) -> int:
    score = 0
    for alias in aliases:
        pattern = rf"(?<![a-z0-9]){re.escape(alias)}(?![a-z0-9])"
        score += len(re.findall(pattern, text))
    return score


def extract_interest_keywords(persona: SourcePersona, limit: int = 10) -> list[str]:
    """Extract keywords ordered by alias occurrence count.

    Higher alias match counts rank first. Alias declaration order is only a
    deterministic tie breaker, so `limit` drops the least-supported keywords.
    """

    text = _persona_text(persona)
    scored_keywords: list[tuple[int, int, str]] = []
    for priority, (keyword, aliases) in enumerate(KEYWORD_ALIASES.items()):
        score = _keyword_score(text, aliases)
        if score > 0:
            scored_keywords.append((-score, priority, keyword))

    if not scored_keywords:
        return ["general"]
    return [keyword for _, _, keyword in sorted(scored_keywords)[:limit]]
