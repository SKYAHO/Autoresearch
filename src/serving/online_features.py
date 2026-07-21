"""Feast SDK와 분리된 온라인 모델 피처 조립 계약."""

from __future__ import annotations

__arch__ = {"stage": "training", "role": "두 단계 온라인 조회와 cold-start 처리로 모델 피처를 조립합니다.",
            "owns": ["15개 모델 피처 순서 계약", "keyed batch 조회 조립", "typed cold-start와 파생 피처", "entity·shape·피처 계약 검증"],
            "not_owns": ["Feast SDK와 Redis bootstrap", "CTR 모델 추론과 점수 정렬", "HTTP 요청·응답 계약"]}

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Final, Protocol, TypeAlias

from src.features.feature_builder import (
    compute_historical_category_match,
    compute_preferred_category_match,
)
from src.serving.schemas import CandidateVideo, FeatureValue

FeatureRows: TypeAlias = Mapping[str, Sequence[object]]

MODEL_FEATURE_COLUMNS: Final[tuple[str, ...]] = (
    "age_group",
    "occupation",
    "historical_category_affinity",
    "recent_click_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "category_id",
    "duration_sec",
    "view_count",
    "like_ratio",
    "comment_ratio",
    "days_since_upload",
    "historical_category_match",
    "preferred_category_match",
    "topic_similarity",
)

_FIRST_READ_FEATURE_REFS: Final[tuple[str, ...]] = (
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
_SECOND_READ_FEATURE_REFS: Final[tuple[str, ...]] = (
    "UserCategorySimilarityView:topic_similarity",
)
_FIRST_READ_COLUMNS: Final[tuple[str, ...]] = (
    "age_group",
    "occupation",
    "preferred_category",
    "historical_category_affinity",
    "recent_click_count_7d",
    "recent_watch_time_7d",
    "recent_like_count_7d",
    "category_id",
    "duration_sec",
    "view_count",
    "like_ratio",
    "comment_ratio",
    "days_since_upload",
)


class OnlineFeatureReader(Protocol):
    """배치 entity 조회를 제공하는 Feast 어댑터의 최소 계약."""

    def read(
        self,
        *,
        feature_refs: Sequence[str],
        entity_rows: Sequence[Mapping[str, str]],
    ) -> FeatureRows:
        """요청 entity 행에 대한 열 지향 피처 결과를 반환한다."""


@dataclass(frozen=True, slots=True)
class FeatureRetrievalError(Exception):
    """피처 조회 결과가 요청 entity 계약과 맞지 않을 때 발생한다."""

    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class FeatureContractError(Exception):
    """모델 artifact 또는 builder 입력 계약이 고정 스키마와 다를 때 발생한다."""

    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class ServingFeatureBuilder:
    """두 번의 keyed batch read로 Reranker 입력을 구성한다."""

    reader: OnlineFeatureReader

    def build(
        self,
        *,
        user_id: str,
        video_ids: Sequence[str],
        feature_columns: Sequence[str],
    ) -> list[CandidateVideo]:
        """모델 artifact 순서를 검증하고 입력 영상 순서의 후보 피처를 반환한다."""
        _validate_build_request(
            user_id=user_id,
            video_ids=video_ids,
            feature_columns=feature_columns,
        )
        first_entities = tuple(
            {"user_id": user_id, "video_id": video_id} for video_id in video_ids
        )
        first_rows = _keyed_rows(
            rows=self.reader.read(
                feature_refs=_FIRST_READ_FEATURE_REFS,
                entity_rows=first_entities,
            ),
            expected_keys=tuple((user_id, video_id) for video_id in video_ids),
            key_names=("user_id", "video_id"),
            required_columns=_FIRST_READ_COLUMNS,
        )

        categories_by_video = {
            video_id: _string_or_default(
                first_rows[(user_id, video_id)]["category_id"], default="unknown"
            )
            for video_id in video_ids
        }
        category_ids = tuple(dict.fromkeys(categories_by_video.values()))
        second_entities = tuple(
            {"user_id": user_id, "category_id": category_id}
            for category_id in category_ids
        )
        similarity_rows = _keyed_rows(
            rows=self.reader.read(
                feature_refs=_SECOND_READ_FEATURE_REFS,
                entity_rows=second_entities,
            ),
            expected_keys=tuple((user_id, category_id) for category_id in category_ids),
            key_names=("user_id", "category_id"),
            required_columns=("topic_similarity",),
        )

        return [
            _candidate_from_row(
                video_id=video_id,
                row=first_rows[(user_id, video_id)],
                category_id=categories_by_video[video_id],
                topic_similarity=similarity_rows[(user_id, categories_by_video[video_id])][
                    "topic_similarity"
                ],
            )
            for video_id in video_ids
        ]


def _validate_build_request(
    *, user_id: str, video_ids: Sequence[str], feature_columns: Sequence[str]
) -> None:
    if tuple(feature_columns) != MODEL_FEATURE_COLUMNS:
        raise FeatureContractError(reason="Model feature columns do not match the serving contract.")
    if not user_id:
        raise FeatureContractError(reason="user_id must not be empty.")
    if not 1 <= len(video_ids) <= 200:
        raise FeatureContractError(reason="video_ids must contain between 1 and 200 items.")
    if any(not video_id for video_id in video_ids):
        raise FeatureContractError(reason="video_ids must not contain empty values.")
    if len(set(video_ids)) != len(video_ids):
        raise FeatureContractError(reason="video_ids must not contain duplicates.")


def _keyed_rows(
    *,
    rows: FeatureRows,
    expected_keys: tuple[tuple[str, str], ...],
    key_names: tuple[str, str],
    required_columns: tuple[str, ...],
) -> Mapping[tuple[str, str], Mapping[str, object]]:
    required = (*key_names, *required_columns)
    missing_columns = tuple(column for column in required if column not in rows)
    if missing_columns:
        raise FeatureRetrievalError(
            reason=f"Feature result is missing required columns: {', '.join(missing_columns)}."
        )

    expected_length = len(expected_keys)
    mismatched_columns = tuple(
        column for column in required if len(rows[column]) != expected_length
    )
    if mismatched_columns:
        raise FeatureRetrievalError(
            reason=f"Feature result has unexpected lengths for: {', '.join(mismatched_columns)}."
        )

    keyed_rows: dict[tuple[str, str], Mapping[str, object]] = {}
    for index in range(expected_length):
        key = (rows[key_names[0]][index], rows[key_names[1]][index])
        if not isinstance(key[0], str) or not isinstance(key[1], str):
            raise FeatureRetrievalError(reason="Feature result entity keys must be strings.")
        if key in keyed_rows:
            raise FeatureRetrievalError(reason="Feature result contains duplicate entity keys.")
        keyed_rows[key] = {column: rows[column][index] for column in required_columns}

    if set(keyed_rows) != set(expected_keys):
        raise FeatureRetrievalError(reason="Feature result entity keys do not match the request.")
    return keyed_rows


def _candidate_from_row(
    *,
    video_id: str,
    row: Mapping[str, object],
    category_id: str,
    topic_similarity: object,
) -> CandidateVideo:
    preferred_category = _preferred_category_or_default(row["preferred_category"])
    historical_category_affinity = _string_or_default(
        row["historical_category_affinity"], default="unknown"
    )
    features: dict[str, FeatureValue] = {
        "age_group": _string_or_default(row["age_group"], default="unknown"),
        "occupation": _string_or_default(row["occupation"], default="unknown"),
        "historical_category_affinity": historical_category_affinity,
        "recent_click_count_7d": _integer_or_default(row["recent_click_count_7d"]),
        "recent_watch_time_7d": _integer_or_default(row["recent_watch_time_7d"]),
        "recent_like_count_7d": _integer_or_default(row["recent_like_count_7d"]),
        "category_id": category_id,
        "duration_sec": _integer_or_default(row["duration_sec"]),
        "view_count": _integer_or_default(row["view_count"]),
        "like_ratio": _float_or_default(row["like_ratio"]),
        "comment_ratio": _float_or_default(row["comment_ratio"]),
        "days_since_upload": _integer_or_default(row["days_since_upload"]),
        "historical_category_match": compute_historical_category_match(
            historical_category_affinity, category_id
        ),
        "preferred_category_match": compute_preferred_category_match(
            preferred_category, category_id
        ),
        "topic_similarity": _float_or_default(topic_similarity),
    }
    return CandidateVideo(video_id=video_id, features=features)


def _string_or_default(value: object, *, default: str) -> str:
    if value is None:
        return default
    if isinstance(value, str):
        return value
    raise FeatureRetrievalError(reason="Feature result contains a non-string categorical value.")


def _integer_or_default(value: object) -> int:
    if value is None:
        return 0
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    raise FeatureRetrievalError(reason="Feature result contains a non-integer count value.")


def _float_or_default(value: object) -> float:
    if value is None:
        return 0.0
    if isinstance(value, float):
        return value
    raise FeatureRetrievalError(reason="Feature result contains a non-float ratio value.")


def _preferred_category_or_default(value: object) -> list[str] | str:
    if value is None:
        return []
    if isinstance(value, str):
        return value
    if isinstance(value, list) and all(isinstance(category_id, str) for category_id in value):
        return value
    raise FeatureRetrievalError(reason="Feature result contains an invalid preferred_category value.")
