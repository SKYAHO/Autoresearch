import pytest

from autoresearch.virtual_users.categories import (
    DEFAULT_KAGGLE_YOUTUBE_CATEGORIES,
    build_category_affinity,
    validate_categories,
)


def test_validate_categories_rejects_non_kaggle_category_name() -> None:
    allowed = set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES)

    with pytest.raises(ValueError):
        validate_categories(["Travel"], allowed)


def test_validate_categories_accepts_exact_kaggle_category_name() -> None:
    allowed = set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES)

    assert validate_categories(["Travel & Events"], allowed) == ["Travel & Events"]


def test_build_category_affinity_uses_rank_and_evidence_scores() -> None:
    allowed = set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES)

    affinity = build_category_affinity(
        primary_categories=["Gaming", "Entertainment", "Music"],
        category_evidence={
            "Gaming": ["닌텐도 스위치", "동물의 숲"],
            "Entertainment": ["넷플릭스"],
            "Music": ["LP"],
        },
        allowed_categories=allowed,
    )

    assert affinity == {
        "Gaming": 0.91,
        "Entertainment": 0.75,
        "Music": 0.63,
    }


def test_build_category_affinity_rejects_duplicate_primary_categories() -> None:
    allowed = set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES)

    with pytest.raises(ValueError, match="Duplicate"):
        build_category_affinity(
            primary_categories=["Gaming", "Gaming"],
            category_evidence={"Gaming": ["닌텐도 스위치"]},
            allowed_categories=allowed,
        )
