from __future__ import annotations

import pytest

from autoresearch.feature_engineering.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
    FeatureContractError,
    require_categorical_feature_columns,
    require_experiment_feature_columns,
    require_model_feature_columns,
    resolve_experiment_feature_columns,
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


# --- 실험용 피처 오버라이드 (#405) ---
# 계약 정본: docs/specs/2026-07-31-experiment-feature-override.md


def test_resolve_experiment_feature_columns_appends_after_prod_contract() -> None:
    """실험 피처는 prod 계약 **뒤에만** 붙는다.

    ONNX 입력 텐서가 이름 없는 순서 배열이라, prod 접두부가 그대로 유지돼야
    기존 아티팩트·서빙 해석이 깨지지 않는다.
    """
    resolved = resolve_experiment_feature_columns(("views_per_day",))

    assert resolved[: len(MODEL_FEATURE_COLUMNS)] == MODEL_FEATURE_COLUMNS
    assert resolved[len(MODEL_FEATURE_COLUMNS) :] == ("views_per_day",)
    assert len(resolved) == len(MODEL_FEATURE_COLUMNS) + 1


def test_resolve_experiment_feature_columns_keeps_extra_order() -> None:
    resolved = resolve_experiment_feature_columns(("b_feat", "a_feat"))

    # 정렬하지 않는다 — 실험자가 준 순서가 곧 텐서 순서다.
    assert resolved[len(MODEL_FEATURE_COLUMNS) :] == ("b_feat", "a_feat")


def test_resolve_experiment_feature_columns_rejects_empty() -> None:
    # 빈 목록으로 실험 경로를 켜면 prod와 동일한데 실험으로 표시된다 — 무의미하고
    # 승격만 막히므로 거부한다.
    with pytest.raises(FeatureContractError):
        resolve_experiment_feature_columns(())


def test_resolve_experiment_feature_columns_rejects_duplicates_within_extra() -> None:
    with pytest.raises(FeatureContractError) as excinfo:
        resolve_experiment_feature_columns(("views_per_day", "views_per_day"))

    assert "views_per_day" in str(excinfo.value)


def test_resolve_experiment_feature_columns_rejects_name_already_in_prod() -> None:
    # 이미 prod 피처면 실험 피처가 아니다. 그대로 두면 같은 이름이 텐서에 두 번
    # 들어가 조용히 오예측한다.
    with pytest.raises(FeatureContractError) as excinfo:
        resolve_experiment_feature_columns((MODEL_FEATURE_COLUMNS[0],))

    assert MODEL_FEATURE_COLUMNS[0] in str(excinfo.value)


def test_require_experiment_feature_columns_accepts_matching_columns() -> None:
    extra = ("views_per_day",)
    columns = resolve_experiment_feature_columns(extra)

    assert require_experiment_feature_columns(list(columns), extra=extra) == columns


def test_require_experiment_feature_columns_rejects_reordered_prod_prefix() -> None:
    extra = ("views_per_day",)
    swapped = (
        MODEL_FEATURE_COLUMNS[1],
        MODEL_FEATURE_COLUMNS[0],
        *MODEL_FEATURE_COLUMNS[2:],
        *extra,
    )

    with pytest.raises(FeatureContractError):
        require_experiment_feature_columns(swapped, extra=extra)


def test_require_experiment_feature_columns_rejects_unexpected_extra() -> None:
    # 아티팩트가 선언과 다른 실험 피처를 담고 있으면 채점 대상이 어긋난다.
    with pytest.raises(FeatureContractError):
        require_experiment_feature_columns(
            resolve_experiment_feature_columns(("views_per_day",)),
            extra=("other_feat",),
        )


def test_require_experiment_feature_columns_rejects_prod_only_columns() -> None:
    with pytest.raises(FeatureContractError):
        require_experiment_feature_columns(
            MODEL_FEATURE_COLUMNS, extra=("views_per_day",)
        )


def test_prod_contract_is_untouched_by_experiment_helpers() -> None:
    """실험 경로가 prod 계약 자체를 바꾸지 않는다(#405 완료조건 1)."""
    before = MODEL_FEATURE_COLUMNS

    resolve_experiment_feature_columns(("views_per_day",))

    assert MODEL_FEATURE_COLUMNS is before
    assert MODEL_FEATURE_COLUMNS == EXPECTED_MODEL_FEATURE_COLUMNS
    # prod 경로의 엄격한 동등 검사는 느슨해지지 않는다.
    with pytest.raises(FeatureContractError):
        require_model_feature_columns(
            resolve_experiment_feature_columns(("views_per_day",))
        )


def test_passthrough_columns_are_disjoint_from_model_input() -> None:
    """패스스루 컬럼은 모델 입력 계약과 겹치지 않는다(#505).

    겹치면 평가용으로 실은 컬럼이 모델 입력으로 새어 들어가 유저 암기가 발생한다.
    """
    from autoresearch.feature_engineering.model_contract import PASSTHROUGH_COLUMNS

    assert "user_id" in PASSTHROUGH_COLUMNS
    assert set(PASSTHROUGH_COLUMNS).isdisjoint(MODEL_FEATURE_COLUMNS)
    assert "clicked" not in PASSTHROUGH_COLUMNS


def test_resolve_experiment_feature_columns_rejects_passthrough_name() -> None:
    """패스스루 이름을 실험 피처로 승격시킬 수 없다(#505)."""
    from autoresearch.feature_engineering.model_contract import PASSTHROUGH_COLUMNS

    with pytest.raises(FeatureContractError):
        resolve_experiment_feature_columns((PASSTHROUGH_COLUMNS[0],))
