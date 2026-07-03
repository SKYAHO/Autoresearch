from autoresearch.virtual_users.categories import DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
from autoresearch.virtual_users.interests import (
    CATEGORY_KEYWORDS,
    extract_interest_keywords,
)
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
        "beauty",
        "study",
        "design",
        "sports",
        "travel",
        "cooking",
        "lifestyle",
    ]


def test_extract_interest_keywords_returns_general_when_no_match():
    persona = SourcePersona(
        uuid="p-002",
        age=26,
        sex="male",
        persona="No clear media preference is present.",
    )

    assert extract_interest_keywords(persona) == ["general"]


def test_interest_category_keywords_use_kaggle_vocab():
    assert set(CATEGORY_KEYWORDS).issubset(set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES))
    assert "Travel & Events" in DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
