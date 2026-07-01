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
    text = _persona_text(persona)
    keywords: list[str] = []
    for keyword, aliases in KEYWORD_ALIASES.items():
        if any(alias in text for alias in aliases):
            keywords.append(keyword)

    if not keywords:
        return ["general"]
    return keywords[:limit]
