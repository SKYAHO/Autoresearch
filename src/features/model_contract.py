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

# 카테고리형 피처의 cold-start 기본값(값 없음 → 'unknown'). 학습(feast_retrieval)과
# 서빙(online_features)이 이 단일 소스를 공유해 두 경로의 cold-start가 어긋나지 않게
# 한다(#358). 계약 계층이 소유하고 두 계층이 import한다(레이어 방향 정렬).
COLD_START_CATEGORICAL_DEFAULT: Final[str] = "unknown"


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


def resolve_experiment_feature_columns(extra: Sequence[str]) -> tuple[str, ...]:
    """prod 계약 뒤에 실험 피처를 덧붙인 모델 입력 순서를 만든다(#405).

    실험 피처 1개를 넣으려고 `MODEL_FEATURE_COLUMNS`를 직접 고치면 학습·서빙·
    시뮬레이션·일일추천이 한꺼번에 깨진다(#396 실측: 테스트 43건). 이 함수는 prod
    계약을 **전혀 건드리지 않고** 실험 전용 순서만 따로 만든다.

    실험 피처는 반드시 **뒤에** 붙인다. ONNX 입력이 이름 없는 순서 배열이라 prod
    접두부가 그대로 유지돼야 기존 아티팩트와 서빙 해석이 깨지지 않는다. 중간 삽입이나
    재정렬은 지원하지 않는다.

    ``extra``의 순서는 정렬하지 않고 그대로 쓴다 — 실험자가 준 순서가 곧 텐서 순서다.

    Args:
        extra: prod 계약에 없는 실험 피처 이름. 데이터셋에 이미 존재하는 컬럼이어야
            하며, 컬럼을 만들어내는 일은 이 경로의 책임이 아니다(#399 / Feast ODFV).

    Returns:
        `MODEL_FEATURE_COLUMNS` + `extra` 순서의 컬럼 튜플.

    Raises:
        FeatureContractError: `extra`가 비었거나, 내부에 중복이 있거나, prod 계약과
            이름이 겹치면.
    """
    requested = tuple(extra)
    if not requested:
        raise FeatureContractError(
            "실험 피처 목록이 비었습니다 — prod 계약과 동일한 입력인데 실험으로만 "
            "표시되어 승격이 막힙니다. 실험 피처를 지정하거나 옵션을 빼십시오."
        )

    duplicated = sorted({name for name in requested if requested.count(name) > 1})
    if duplicated:
        raise FeatureContractError(
            f"실험 피처에 중복이 있습니다: {duplicated}. "
            "같은 이름이 입력 텐서에 두 번 들어가면 조용히 오예측합니다."
        )

    already_prod = [name for name in requested if name in MODEL_FEATURE_COLUMNS]
    if already_prod:
        raise FeatureContractError(
            f"실험 피처 {already_prod}는 이미 prod 계약(MODEL_FEATURE_COLUMNS)에 "
            "있습니다. 실험 피처는 계약에 없는 컬럼만 지정할 수 있습니다."
        )

    return MODEL_FEATURE_COLUMNS + requested


def require_experiment_feature_columns(
    columns: Sequence[str], *, extra: Sequence[str]
) -> tuple[str, ...]:
    """실험 경로의 모델 입력 순서를 검증한다(#405).

    prod 경로의 `require_model_feature_columns()`는 계약과의 **정확한 동등**을 요구하며
    그 엄격함은 유지된다. 이 함수는 실험 경로 전용으로, "앞부분은 prod 계약 그대로 +
    뒷부분은 선언한 실험 피처 그대로"를 요구한다.

    Args:
        columns: 검증할 컬럼 순서(보통 `feature_columns.json` 아티팩트).
        extra: 이 실행이 선언한 실험 피처.

    Returns:
        검증을 통과한 컬럼 튜플.

    Raises:
        FeatureContractError: 순서가 기대와 다르면.
    """
    expected = resolve_experiment_feature_columns(extra)
    actual = tuple(columns)
    if actual != expected:
        raise FeatureContractError(
            _contract_mismatch_message("Experiment feature", expected, actual),
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
