"""rerank API 클라이언트·lazy 노출 provider 테스트 — 가짜 session, 실 HTTP 없음."""

import random

import pytest
import requests

import src.pipeline.rerank_api as rerank_api
from src.pipeline.rerank_api import (
    RerankApiError,
    RerankApiSettings,
    make_rerank_api_exposure_provider,
    rank_user,
    select_candidate_video_ids,
)


class _FakeResponse:
    def __init__(self, status_code: int = 200, payload: object = None) -> None:
        self.status_code = status_code
        self._payload = payload

    def json(self) -> object:
        return self._payload


class _FakeSession:
    """미리 정한 결과(응답 또는 예외)를 순서대로 돌려주는 session 대역."""

    def __init__(self, outcomes: list) -> None:
        self.outcomes = list(outcomes)
        self.calls: list[dict] = []

    def post(self, url: str, json=None, timeout=None) -> _FakeResponse:
        self.calls.append({"url": url, "json": json, "timeout": timeout})
        outcome = self.outcomes.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def _ok_payload(video_scores: dict[str, float], model_id: str = "run-x") -> dict:
    return {
        "items": [
            {"video_id": vid, "ctr_score": score, "model_id": model_id}
            for vid, score in video_scores.items()
        ]
    }


def _settings(**overrides) -> RerankApiSettings:
    defaults = dict(base_url="http://fake:8000", timeout_sec=5.0, max_attempts=3)
    defaults.update(overrides)
    return RerankApiSettings(**defaults)


@pytest.fixture(autouse=True)
def _no_sleep(monkeypatch):
    monkeypatch.setattr(rerank_api.time, "sleep", lambda seconds: None)


def _videos(n: int = 30) -> list[dict]:
    # view_count를 역순으로 줘서 v000이 최다 조회가 되게 한다.
    return [{"video_id": f"v{i:03d}", "view_count": 1000 - i} for i in range(n)]


def test_rank_user_orders_by_score_desc_then_video_id():
    session = _FakeSession(
        [_FakeResponse(payload=_ok_payload({"vA": 0.1, "vC": 0.9, "vB": 0.9}))]
    )
    ranking, model_id = rank_user(
        _settings(), "u1", ["vA", "vB", "vC"], session=session
    )
    assert model_id == "run-x"
    assert [(r.video_id, r.rank) for r in ranking] == [("vB", 1), ("vC", 2), ("vA", 3)]
    assert ranking[0].ctr_score == 0.9
    assert session.calls[0]["url"] == "http://fake:8000/rerank"
    assert session.calls[0]["json"] == {"user_id": "u1", "video_ids": ["vA", "vB", "vC"]}


def test_select_candidate_video_ids_caps_and_orders_deterministically():
    videos = _videos(250)
    videos.append({"video_id": "v000", "view_count": 1})  # 중복은 첫 항목만
    tie_a = {"video_id": "zzz", "view_count": 1000}
    tie_b = {"video_id": "aaa", "view_count": 1000}
    selected = select_candidate_video_ids([tie_a, tie_b, *videos])
    assert len(selected) == 200
    # 동률(view_count=1000)은 video_id 오름차순: aaa < v000 < zzz
    assert selected[:3] == ["aaa", "v000", "zzz"]
    assert len(set(selected)) == 200


def test_rank_user_retries_connection_error_and_5xx_then_succeeds():
    session = _FakeSession(
        [
            requests.ConnectionError("boom"),
            _FakeResponse(status_code=503),
            _FakeResponse(payload=_ok_payload({"vA": 0.5})),
        ]
    )
    ranking, _ = rank_user(_settings(), "u1", ["vA"], session=session)
    assert len(session.calls) == 3
    assert ranking[0].video_id == "vA"


def test_rank_user_fails_after_retry_budget_exhausted():
    session = _FakeSession([_FakeResponse(status_code=500)] * 3)
    with pytest.raises(RerankApiError, match="3회"):
        rank_user(_settings(), "u1", ["vA"], session=session)
    assert len(session.calls) == 3


def test_rank_user_rejects_4xx_immediately_without_retry():
    session = _FakeSession([_FakeResponse(status_code=422)])
    with pytest.raises(RerankApiError, match="422"):
        rank_user(_settings(), "u1", ["vA"], session=session)
    assert len(session.calls) == 1


def test_rank_user_rejects_mixed_model_id_within_response():
    payload = {
        "items": [
            {"video_id": "vA", "ctr_score": 0.5, "model_id": "run-x"},
            {"video_id": "vB", "ctr_score": 0.4, "model_id": "run-y"},
        ]
    }
    with pytest.raises(RerankApiError, match="혼재"):
        rank_user(_settings(), "u1", ["vA", "vB"], session=_FakeSession([_FakeResponse(payload=payload)]))


def test_rank_user_rejects_empty_items():
    with pytest.raises(RerankApiError, match="비어"):
        rank_user(
            _settings(), "u1", ["vA"],
            session=_FakeSession([_FakeResponse(payload={"items": []})]),
        )


def test_provider_assembles_tagged_exposures_from_api_ranking():
    videos = _videos(30)
    candidate_ids = select_candidate_video_ids(videos)
    scores = {vid: 1.0 - i * 0.01 for i, vid in enumerate(candidate_ids)}
    session = _FakeSession([_FakeResponse(payload=_ok_payload(scores))])

    round_ = make_rerank_api_exposure_provider(_settings(), videos, session=session)
    candidates = round_.provider({"user_id": "u1"}, random.Random(42))

    assert len(candidates) == 24
    assert round_.model_run_id == "run-x"
    sources = [
        meta.exposure_source
        for (user_id, _), meta in round_.metadata.items()
        if user_id == "u1"
    ]
    assert sources.count("model") == 17
    assert sources.count("trending") == 5
    assert sources.count("random") == 2
    assert all(meta.policy_version == "run-x" for meta in round_.metadata.values())


def test_provider_fails_when_model_id_changes_mid_round():
    videos = _videos(30)
    candidate_ids = select_candidate_video_ids(videos)
    scores = {vid: 0.5 for vid in candidate_ids}
    session = _FakeSession(
        [
            _FakeResponse(payload=_ok_payload(scores, model_id="run-x")),
            _FakeResponse(payload=_ok_payload(scores, model_id="run-y")),
        ]
    )
    round_ = make_rerank_api_exposure_provider(_settings(), videos, session=session)
    round_.provider({"user_id": "u1"}, random.Random(1))
    with pytest.raises(RerankApiError, match="바뀌"):
        round_.provider({"user_id": "u2"}, random.Random(2))


def test_provider_requires_video_pool():
    with pytest.raises(RerankApiError, match="pool"):
        make_rerank_api_exposure_provider(_settings(), [], session=_FakeSession([]))
