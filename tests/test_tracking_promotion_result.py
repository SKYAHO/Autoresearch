from __future__ import annotations

from src.tracking.promotion_result import (
    ModelPromotionResult,
    PromotionOutcome,
    PromotionReasonCode,
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
