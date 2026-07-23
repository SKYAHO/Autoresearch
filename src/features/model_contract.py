"""Canonical model feature-column contracts for CTR inference.

Defines the ordered model input columns and categorical subset used by model
training, Feast, and artifact I/O.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

MODEL_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
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
CATEGORICAL_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "age_group",
    "occupation",
    "watch_time_band",
    "historical_category_affinity",
    "category_id",
)


@dataclass(frozen=True, slots=True)
class FeatureContractError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


def _contract_mismatch_message(
    contract_name: str,
    expected: tuple[str, ...],
    actual: tuple[str, ...],
) -> str:
    mismatch_position = next(
        (
            position
            for position, (expected_column, actual_column) in enumerate(
                zip(expected, actual, strict=False),
            )
            if expected_column != actual_column
        ),
        min(len(expected), len(actual)),
    )
    expected_column = (
        expected[mismatch_position]
        if mismatch_position < len(expected)
        else "<missing>"
    )
    actual_column = (
        actual[mismatch_position]
        if mismatch_position < len(actual)
        else "<missing>"
    )
    return (
        f"{contract_name} columns do not match the canonical contract at "
        f"zero-based position {mismatch_position}: expected "
        f"{expected_column!r}, got {actual_column!r}. Expected columns: "
        f"{expected!r}; actual columns: {actual!r}."
    )


def require_model_feature_columns(columns: Sequence[str]) -> tuple[str, ...]:
    actual = tuple(columns)
    if actual != MODEL_FEATURE_COLUMNS:
        raise FeatureContractError(
            _contract_mismatch_message("Model feature", MODEL_FEATURE_COLUMNS, actual),
        )
    return actual


def require_categorical_feature_columns(columns: Sequence[str]) -> tuple[str, ...]:
    actual = tuple(columns)
    if actual != CATEGORICAL_FEATURE_COLUMNS:
        raise FeatureContractError(
            _contract_mismatch_message(
                "Categorical feature",
                CATEGORICAL_FEATURE_COLUMNS,
                actual,
            ),
        )
    return actual
