from __future__ import annotations

import pytest

from scripts.verify_serving_e2e import (
    RerankResponse,
    SmokeCheckError,
    _validate_metrics,
    _validate_rerank,
)


def _response(*video_ids: str) -> RerankResponse:
    return RerankResponse.model_validate(
        {
            "items": [
                {
                    "video_id": video_id,
                    "ctr_score": 0.25 + index / 10,
                    "model_id": "run-123",
                }
                for index, video_id in enumerate(video_ids)
            ]
        }
    )


def test_validate_rerank_accepts_full_candidate_response() -> None:
    response = _validate_rerank(
        _response("video-1", "video-2"),
        requested_video_ids=("video-1", "video-2"),
        expected_count=2,
        expected_model_id="run-123",
    )

    assert tuple(item.video_id for item in response.items) == ("video-1", "video-2")


def test_validate_rerank_accepts_expected_top_k_subset() -> None:
    response = _validate_rerank(
        _response("video-2"),
        requested_video_ids=("video-1", "video-2"),
        expected_count=1,
        expected_model_id="run-123",
    )

    assert len(response.items) == 1


def test_validate_rerank_rejects_unrequested_video() -> None:
    with pytest.raises(SmokeCheckError, match="unrequested video"):
        _validate_rerank(
            _response("video-3"),
            requested_video_ids=("video-1",),
            expected_count=1,
            expected_model_id=None,
        )


def test_validate_rerank_rejects_invalid_ctr_score() -> None:
    with pytest.raises(SmokeCheckError, match="invalid CTR"):
        _validate_rerank(
            RerankResponse.model_validate(
                {
                    "items": [
                        {
                            "video_id": "video-1",
                            "ctr_score": 1.1,
                            "model_id": "run-123",
                        }
                    ]
                }
            ),
            requested_video_ids=("video-1",),
            expected_count=1,
            expected_model_id=None,
        )


def test_validate_metrics_requires_serving_counters() -> None:
    _validate_metrics(
        "# HELP rerank_requests_total requests\n"
        "rerank_requests_total 1\n"
        "rerank_video_ids_count 1\n"
    )

    with pytest.raises(SmokeCheckError, match="missing required metric"):
        _validate_metrics("# HELP rerank_requests_total requests\n")
