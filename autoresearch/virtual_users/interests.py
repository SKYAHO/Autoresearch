"""Persona 텍스트에서 추천 후보 매칭에 쓸 관심 keyword를 추출한다."""

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


def extract_interest_keywords(persona: SourcePersona, limit: int = 10) -> list[str]:
    """미리 정의한 alias 사전으로 persona 관심 keyword를 결정적으로 추출한다."""

    text = _persona_text(persona)
    keywords: list[str] = []
    for keyword, aliases in KEYWORD_ALIASES.items():
        if any(alias in text for alias in aliases):
            keywords.append(keyword)

    if not keywords:
        return ["general"]
    return keywords[:limit]
