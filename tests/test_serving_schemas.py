import pytest
from pydantic import ValidationError

from src.serving.schemas import RerankRequest, RerankResponse, RerankResponseItem


def test_rerank_request_accepts_user_and_video_ids_only() -> None:
    request = RerankRequest(user_id="user-1", video_ids=["video-1", "video-2"])

    assert request.model_dump() == {
        "user_id": "user-1",
        "video_ids": ["video-1", "video-2"],
    }


@pytest.mark.parametrize(
    "payload",
    [
        {"user_id": "user-1", "video_ids": ["video-1"] * 201},
        {"user_id": "user-1", "video_ids": ["video-1", "video-1"]},
        {
            "user_id": "user-1",
            "video_ids": ["video-1"],
            "candidates": [{"video_id": "video-1", "features": {"x": 1}}],
        },
    ],
)
def test_rerank_request_rejects_invalid_or_legacy_payload(payload: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        RerankRequest.model_validate(payload)


def test_response_item_contains_model_id_and_no_user_id() -> None:
    response = RerankResponse(
        items=[RerankResponseItem(video_id="video-1", ctr_score=0.7, model_id="run-1")]
    )

    assert response.model_dump() == {
        "items": [{"video_id": "video-1", "ctr_score": 0.7, "model_id": "run-1"}]
    }
