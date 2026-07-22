from __future__ import annotations

import pytest

from src.features.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
    require_categorical_feature_columns,
    require_model_feature_columns,
)

EXPECTED_MODEL_FEATURE_COLUMNS = (
    "age_group",
    "occupation",
    "watch_time_band",
    "recent_click_count_7d",
    "recent_view_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "historical_category_affinity",
    "total_event_count_7d",
    "category_id",
    "duration_sec",
    "view_count",
    "like_ratio",
    "comment_ratio",
    "days_since_upload",
    "channel_subscriber_count",
    "channel_view_count",
    "channel_video_count",
    "topic_similarity",
    "preferred_category_match",
    "historical_category_match",
)


def test_model_feature_contract_has_canonical_order() -> None:
    assert MODEL_FEATURE_COLUMNS == EXPECTED_MODEL_FEATURE_COLUMNS
    assert len(MODEL_FEATURE_COLUMNS) == len(set(MODEL_FEATURE_COLUMNS)) == 21


def test_categorical_contract_is_ordered_subset() -> None:
    assert CATEGORICAL_FEATURE_COLUMNS == (
        "age_group",
        "occupation",
        "watch_time_band",
        "historical_category_affinity",
        "category_id",
    )
    assert set(CATEGORICAL_FEATURE_COLUMNS) < set(MODEL_FEATURE_COLUMNS)


def test_contract_rejects_missing_or_reordered_columns() -> None:
    with pytest.raises(FeatureContractError) as missing_error:
        require_model_feature_columns(MODEL_FEATURE_COLUMNS[:-1])
    assert "zero-based position 20" in str(missing_error.value)
    assert "expected 'historical_category_match'" in str(missing_error.value)
    assert "got '<missing>'" in str(missing_error.value)

    with pytest.raises(FeatureContractError) as reordered_error:
        require_model_feature_columns(tuple(reversed(MODEL_FEATURE_COLUMNS)))
    assert "zero-based position 0" in str(reordered_error.value)
    assert "expected 'age_group'" in str(reordered_error.value)
    assert "got 'historical_category_match'" in str(reordered_error.value)

    with pytest.raises(FeatureContractError):
        require_categorical_feature_columns(CATEGORICAL_FEATURE_COLUMNS[:-1])
