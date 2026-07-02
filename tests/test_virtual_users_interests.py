from autoresearch.virtual_users.interests import extract_interest_keywords
from autoresearch.virtual_users.schema import SourcePersona


def test_extract_interest_keywords_uses_spec_persona_fields():
    persona = SourcePersona(
        uuid="p-001",
        age=24,
        sex="female",
        persona="Enjoys music videos and lifestyle creators.",
        hobbies_and_interests="beauty, study videos",
        hobbies_and_interests_list=["music", "beauty"],
        professional_persona="Early career learner.",
        skills_and_expertise="design and presentation",
        sports_persona="Light sports viewer.",
        arts_persona="Popular music fan.",
        travel_persona="Cafe trip videos.",
        culinary_persona="Cooking shorts.",
        family_persona="Family lifestyle.",
    )

    keywords = extract_interest_keywords(persona)

    assert keywords == [
        "music",
        "lifestyle",
        "beauty",
        "study",
        "design",
        "travel",
        "sports",
        "cooking",
    ]


def test_extract_interest_keywords_applies_limit_after_relevance_sort():
    persona = SourcePersona(
        uuid="p-003",
        age=27,
        sex="male",
        persona="Gaming gaming gaming music videos.",
        hobbies_and_interests="technology and gaming",
        skills_and_expertise="software developer",
    )

    assert extract_interest_keywords(persona, limit=2) == ["gaming", "technology"]


def test_extract_interest_keywords_returns_general_when_no_match():
    persona = SourcePersona(
        uuid="p-002",
        age=26,
        sex="male",
        persona="No clear media preference is present.",
    )

    assert extract_interest_keywords(persona) == ["general"]
