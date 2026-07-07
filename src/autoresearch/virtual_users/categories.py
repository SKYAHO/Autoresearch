"""Category vocabulary validation and deterministic affinity scoring."""

DEFAULT_KAGGLE_YOUTUBE_CATEGORIES = [
    "Film & Animation",
    "Autos & Vehicles",
    "Music",
    "Pets & Animals",
    "Sports",
    "Travel & Events",
    "Gaming",
    "People & Blogs",
    "Comedy",
    "Entertainment",
    "News & Politics",
    "Howto & Style",
    "Education",
    "Science & Technology",
    "Nonprofits & Activism",
]

_RANK_BASES = {
    1: 0.85,
    2: 0.72,
    3: 0.60,
    4: 0.50,
    5: 0.42,
}


def validate_categories(categories: list[str], allowed: set[str]) -> list[str]:
    """Return categories when all names are in the allowed vocabulary."""
    invalid_categories = [category for category in categories if category not in allowed]
    if invalid_categories:
        raise ValueError(f"Unsupported categories: {invalid_categories}")
    if len(set(categories)) != len(categories):
        raise ValueError("Duplicate categories are not allowed")
    return categories


def build_category_affinity(
    primary_categories: list[str],
    category_evidence: dict[str, list[str]],
    allowed_categories: set[str],
) -> dict[str, float]:
    """Build deterministic YouTube category affinity scores."""
    validate_categories(primary_categories, allowed_categories)
    validate_categories(list(category_evidence), allowed_categories)

    affinity: dict[str, float] = {}
    for rank, category in enumerate(primary_categories, start=1):
        if rank not in _RANK_BASES:
            break
        evidence_count = len(category_evidence.get(category, []))
        evidence_boost = min(0.10, evidence_count * 0.03)
        affinity[category] = round(min(0.95, _RANK_BASES[rank] + evidence_boost), 2)
    return affinity
