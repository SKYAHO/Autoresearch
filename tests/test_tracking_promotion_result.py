from __future__ import annotations

import json
import math

import pytest
from pydantic import ValidationError

from autoresearch.model_registry import promotion_result
from autoresearch.model_registry.promotion_result import (
    ModelPromotionResult,
    PromotionOutcome,
    PromotionReasonCode,
    write_result_file,
)


def test_result_serializes_exact_v1_envelope() -> None:
    result = ModelPromotionResult(
        outcome=PromotionOutcome.REJECTED,
        model_name="ctr-model",
        champion_alias="champion",
        candidate_version="13",
        champion_version="12",
        candidate_metric=0.7812,
        champion_metric=0.7931,
        reason_code=PromotionReasonCode.METRIC_BELOW_CHAMPION,
    )

    assert result.model_dump(mode="json") == {
        "event": "model_promotion_result",
        "contract_version": "model-promotion-result-v1",
        "outcome": "rejected",
        "model_name": "ctr-model",
        "champion_alias": "champion",
        "candidate_version": "13",
        "champion_version": "12",
        "metric_name": "val_roc_auc",
        "candidate_metric": 0.7812,
        "champion_metric": 0.7931,
        "reason_code": "metric_below_champion",
    }


@pytest.mark.parametrize("metric", [math.nan, math.inf, -math.inf])
def test_result_rejects_non_finite_metric(metric: float) -> None:
    with pytest.raises(ValidationError):
        ModelPromotionResult(
            outcome=PromotionOutcome.PROMOTED,
            model_name="ctr-model",
            champion_alias="champion",
            candidate_version="13",
            candidate_metric=metric,
            reason_code=PromotionReasonCode.FIRST_CHAMPION,
        )


def test_result_rejects_invalid_outcome_reason_pair() -> None:
    with pytest.raises(ValidationError):
        ModelPromotionResult(
            outcome=PromotionOutcome.PROMOTED,
            model_name="ctr-model",
            champion_alias="champion",
            candidate_version="13",
            candidate_metric=0.81,
            reason_code=PromotionReasonCode.METRIC_BELOW_CHAMPION,
        )


def _promoted_result() -> ModelPromotionResult:
    return ModelPromotionResult(
        outcome=PromotionOutcome.PROMOTED,
        model_name="ctr-model",
        champion_alias="champion",
        candidate_version="13",
        champion_version="12",
        candidate_metric=0.81,
        champion_metric=0.80,
        reason_code=PromotionReasonCode.METRIC_NOT_DEGRADED,
    )


def test_write_result_file_creates_parent_and_writes_one_json_object(
    tmp_path,
) -> None:
    target = tmp_path / "xcom" / "return.json"

    write_result_file(_promoted_result(), target)

    assert json.loads(target.read_text(encoding="utf-8"))["outcome"] == "promoted"
    assert list(target.parent.glob(f".{target.name}.*.tmp")) == []


def test_write_result_file_atomically_replaces_existing_target(tmp_path) -> None:
    target = tmp_path / "return.json"
    target.write_text('{"outcome":"old"}', encoding="utf-8")

    write_result_file(_promoted_result(), target)

    assert json.loads(target.read_text(encoding="utf-8"))["outcome"] == "promoted"


def test_write_result_file_cleans_temporary_file_when_replace_fails(
    monkeypatch,
    tmp_path,
) -> None:
    target = tmp_path / "return.json"

    def _fail_replace(_source, _target) -> None:
        raise OSError("replace failed")

    monkeypatch.setattr(promotion_result.os, "replace", _fail_replace)

    with pytest.raises(OSError, match="replace failed"):
        write_result_file(_promoted_result(), target)

    assert not target.exists()
    assert list(tmp_path.glob(f".{target.name}.*.tmp")) == []
