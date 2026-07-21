from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field

import pytest

from src.serving.online_features import (
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
    FeatureRetrievalError,
    ServingFeatureBuilder,
)


FIRST_READ_FEATURE_REFS = (
    "UserStaticView:age_group",
    "UserStaticView:occupation",
    "UserStaticView:preferred_category",
    "UserDynamicView:historical_category_affinity",
    "UserDynamicView:recent_click_count_7d",
    "UserDynamicView:recent_watch_time_7d",
    "UserDynamicView:recent_like_count_7d",
    "VideoFeatureView:category_id",
    "VideoFeatureView:duration_sec",
    "VideoFeatureView:view_count",
    "VideoFeatureView:like_ratio",
    "VideoFeatureView:comment_ratio",
    "VideoFeatureView:days_since_upload",
)
SECOND_READ_FEATURE_REFS = ("UserCategorySimilarityView:topic_similarity",)


@dataclass
class FakeReader:
    responses: list[Mapping[str, Sequence[object]]]
    calls: list[tuple[tuple[str, ...], tuple[dict[str, str], ...]]] = field(
        default_factory=list
    )

    def read(
        self,
        *,
        feature_refs: Sequence[str],
        entity_rows: Sequence[Mapping[str, str]],
    ) -> Mapping[str, Sequence[object]]:
        self.calls.append(
            (tuple(feature_refs), tuple(dict(entity_row) for entity_row in entity_rows))
        )
        return self.responses.pop(0)


def _first_response() -> Mapping[str, Sequence[object]]:
    return {
        "user_id": ["user-1", "user-1", "user-1"],
        "video_id": ["video-b", "video-c", "video-a"],
        "age_group": ["25-34", "25-34", "25-34"],
        "occupation": ["engineer", "engineer", "engineer"],
        "preferred_category": [["10"], ["10"], ["10"]],
        "historical_category_affinity": ["10", "10", "10"],
        "recent_click_count_7d": [4, 4, 4],
        "recent_watch_time_7d": [30, 30, 30],
        "recent_like_count_7d": [2, 2, 2],
        "category_id": ["10", "20", "10"],
        "duration_sec": [40, 50, 60],
        "view_count": [400, 500, 600],
        "like_ratio": [0.4, 0.5, 0.6],
        "comment_ratio": [0.04, 0.05, 0.06],
        "days_since_upload": [4, 5, 6],
    }


def _similarity_response() -> Mapping[str, Sequence[object]]:
    return {
        "user_id": ["user-1", "user-1"],
        "category_id": ["20", "10"],
        "topic_similarity": [0.2, 0.1],
    }


def test_build_uses_two_keyed_batch_reads_and_returns_ordered_model_features() -> None:
    reader = FakeReader(responses=[_first_response(), _similarity_response()])
    builder = ServingFeatureBuilder(reader=reader)

    candidates = builder.build(
        user_id="user-1",
        video_ids=["video-a", "video-b", "video-c"],
        feature_columns=MODEL_FEATURE_COLUMNS,
    )

    assert reader.calls == [
        (
            FIRST_READ_FEATURE_REFS,
            (
                {"user_id": "user-1", "video_id": "video-a"},
                {"user_id": "user-1", "video_id": "video-b"},
                {"user_id": "user-1", "video_id": "video-c"},
            ),
        ),
        (
            SECOND_READ_FEATURE_REFS,
            (
                {"user_id": "user-1", "category_id": "10"},
                {"user_id": "user-1", "category_id": "20"},
            ),
        ),
    ]
    assert [candidate.video_id for candidate in candidates] == [
        "video-a",
        "video-b",
        "video-c",
    ]
    assert [tuple(candidate.features) for candidate in candidates] == [
        MODEL_FEATURE_COLUMNS,
        MODEL_FEATURE_COLUMNS,
        MODEL_FEATURE_COLUMNS,
    ]
    assert [len(candidate.features) for candidate in candidates] == [15, 15, 15]
    assert candidates[0].features == {
        "age_group": "25-34",
        "occupation": "engineer",
        "historical_category_affinity": "10",
        "recent_click_count_7d": 4,
        "recent_watch_time_7d": 30,
        "recent_like_count_7d": 2,
        "category_id": "10",
        "duration_sec": 60,
        "view_count": 600,
        "like_ratio": 0.6,
        "comment_ratio": 0.06,
        "days_since_upload": 6,
        "historical_category_match": 1,
        "preferred_category_match": 1,
        "topic_similarity": 0.1,
    }
    assert candidates[1].features["topic_similarity"] == 0.1
    assert candidates[2].features["topic_similarity"] == 0.2


def test_build_applies_typed_cold_start_defaults_before_derived_features() -> None:
    reader = FakeReader(
        responses=[
            {
                "user_id": ["user-1"],
                "video_id": ["video-1"],
                "age_group": [None],
                "occupation": [None],
                "preferred_category": [None],
                "historical_category_affinity": [None],
                "recent_click_count_7d": [None],
                "recent_watch_time_7d": [None],
                "recent_like_count_7d": [None],
                "category_id": [None],
                "duration_sec": [None],
                "view_count": [None],
                "like_ratio": [None],
                "comment_ratio": [None],
                "days_since_upload": [None],
            },
            {
                "user_id": ["user-1"],
                "category_id": ["unknown"],
                "topic_similarity": [None],
            },
        ]
    )

    candidate = ServingFeatureBuilder(reader=reader).build(
        user_id="user-1",
        video_ids=["video-1"],
        feature_columns=MODEL_FEATURE_COLUMNS,
    )[0]

    assert candidate.features == {
        "age_group": "unknown",
        "occupation": "unknown",
        "historical_category_affinity": "unknown",
        "recent_click_count_7d": 0,
        "recent_watch_time_7d": 0,
        "recent_like_count_7d": 0,
        "category_id": "unknown",
        "duration_sec": 0,
        "view_count": 0,
        "like_ratio": 0.0,
        "comment_ratio": 0.0,
        "days_since_upload": 0,
        "historical_category_match": 0,
        "preferred_category_match": 0,
        "topic_similarity": 0.0,
    }


def test_build_shares_unknown_category_second_read_across_cold_start_candidates() -> None:
    reader = FakeReader(
        responses=[
            {
                "user_id": ["user-1", "user-1"],
                "video_id": ["video-b", "video-a"],
                "age_group": [None, None],
                "occupation": [None, None],
                "preferred_category": [None, None],
                "historical_category_affinity": [None, None],
                "recent_click_count_7d": [None, None],
                "recent_watch_time_7d": [None, None],
                "recent_like_count_7d": [None, None],
                "category_id": [None, None],
                "duration_sec": [None, None],
                "view_count": [None, None],
                "like_ratio": [None, None],
                "comment_ratio": [None, None],
                "days_since_upload": [None, None],
            },
            {
                "user_id": ["user-1"],
                "category_id": ["unknown"],
                "topic_similarity": [None],
            },
        ]
    )

    candidates = ServingFeatureBuilder(reader=reader).build(
        user_id="user-1",
        video_ids=["video-a", "video-b"],
        feature_columns=MODEL_FEATURE_COLUMNS,
    )

    assert reader.calls[1] == (
        SECOND_READ_FEATURE_REFS,
        ({"user_id": "user-1", "category_id": "unknown"},),
    )
    assert [candidate.video_id for candidate in candidates] == ["video-a", "video-b"]
    assert [
        (
            candidate.features["category_id"],
            candidate.features["topic_similarity"],
            candidate.features["historical_category_match"],
            candidate.features["preferred_category_match"],
        )
        for candidate in candidates
    ] == [("unknown", 0.0, 0, 0), ("unknown", 0.0, 0, 0)]


def test_build_rejects_first_read_length_mismatch() -> None:
    complete_response = _first_response()
    builder = ServingFeatureBuilder(
        reader=FakeReader(
            responses=[
                {column: values[:-1] for column, values in complete_response.items()},
                _similarity_response(),
            ]
        )
    )

    with pytest.raises(FeatureRetrievalError, match="unexpected lengths"):
        builder.build(
            user_id="user-1",
            video_ids=["video-a", "video-b", "video-c"],
            feature_columns=MODEL_FEATURE_COLUMNS,
        )


def test_build_rejects_first_read_entity_key_mismatch() -> None:
    first_response = _first_response()
    first_response["video_id"] = ["video-a", "video-b", "unexpected-video"]
    builder = ServingFeatureBuilder(
        reader=FakeReader(responses=[first_response, _similarity_response()])
    )

    with pytest.raises(FeatureRetrievalError, match="entity keys do not match"):
        builder.build(
            user_id="user-1",
            video_ids=["video-a", "video-b", "video-c"],
            feature_columns=MODEL_FEATURE_COLUMNS,
        )


def test_build_rejects_second_read_length_mismatch() -> None:
    complete_response = _similarity_response()
    builder = ServingFeatureBuilder(
        reader=FakeReader(
            responses=[
                _first_response(),
                {column: values[:-1] for column, values in complete_response.items()},
            ]
        )
    )

    with pytest.raises(FeatureRetrievalError, match="unexpected lengths"):
        builder.build(
            user_id="user-1",
            video_ids=["video-a", "video-b", "video-c"],
            feature_columns=MODEL_FEATURE_COLUMNS,
        )


def test_build_rejects_second_read_entity_key_mismatch() -> None:
    builder = ServingFeatureBuilder(
        reader=FakeReader(
            responses=[
                _first_response(),
                {
                    "user_id": ["user-1", "user-1"],
                    "category_id": ["10", "unexpected-category"],
                    "topic_similarity": [0.1, 0.2],
                },
            ]
        )
    )

    with pytest.raises(FeatureRetrievalError, match="entity keys do not match"):
        builder.build(
            user_id="user-1",
            video_ids=["video-a", "video-b", "video-c"],
            feature_columns=MODEL_FEATURE_COLUMNS,
        )


def test_build_rejects_mismatched_model_artifact_columns() -> None:
    builder = ServingFeatureBuilder(reader=FakeReader(responses=[]))

    with pytest.raises(FeatureContractError):
        builder.build(
            user_id="user-1",
            video_ids=["video-1"],
            feature_columns=MODEL_FEATURE_COLUMNS[:-1],
        )


def test_build_rejects_duplicate_video_ids() -> None:
    builder = ServingFeatureBuilder(reader=FakeReader(responses=[]))

    with pytest.raises(FeatureContractError):
        builder.build(
            user_id="user-1",
            video_ids=["video-1", "video-1"],
            feature_columns=MODEL_FEATURE_COLUMNS,
        )
