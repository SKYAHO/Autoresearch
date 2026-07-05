import pytest

from autoresearch.youtube_collection.client import Verdict, _classify_error


@pytest.mark.parametrize(
    "status,reason,expected",
    [
        # TERMINAL_QUOTA — 프로젝트 단위
        (403, "quotaExceeded", Verdict.TERMINAL_QUOTA),
        (403, "dailyLimitExceeded", Verdict.TERMINAL_QUOTA),
        # userRateLimitExceeded — 보수적 회전 무효(같은 Key backoff)
        (403, "userRateLimitExceeded", Verdict.BACKOFF),
        # API/글로벌 레이트리밋
        (403, "rateLimitExceeded", Verdict.BACKOFF),
        (429, "rateLimitExceeded", Verdict.BACKOFF),
        (403, "servingLimitExceeded", Verdict.BACKOFF),
        (403, "concurrentLimitExceeded", Verdict.BACKOFF),
        (403, "limitExceeded", Verdict.BACKOFF),
        # ROTATE — Key 자체 무효/만료
        (400, "keyInvalid", Verdict.ROTATE),
        (400, "keyExpired", Verdict.ROTATE),
        (401, "unauthorized", Verdict.ROTATE),
        (401, "authError", Verdict.ROTATE),
        (401, "required", Verdict.ROTATE),
        (401, "expired", Verdict.ROTATE),
        # TERMINAL_CONFIG — 프로젝트 스코프
        (403, "accessNotConfigured", Verdict.TERMINAL_CONFIG),
        # 5xx / 네트워크 → 일시적
        (500, "internalError", Verdict.BACKOFF),
        (503, "backendError", Verdict.BACKOFF),
        (503, "notReady", Verdict.BACKOFF),
        # malformed/unknown → 일시적 기본
        (503, None, Verdict.BACKOFF),
        (403, "someUndefinedReason", Verdict.IP_BAN_CANDIDATE),  # 기타 403
        (403, "", Verdict.IP_BAN_CANDIDATE),  # 빈 reason 403
    ],
)
def test_classify_error_maps_youtube_reasons(status, reason, expected):
    assert _classify_error(status, reason) is expected


from autoresearch.youtube_collection.client import (
    ResilientYouTubeClient,
    YouTubeCallables,
)


def _fake_videos_response():
    return {"items": [{"id": "v1"}], "nextPageToken": None}


def test_make_callables_returns_named_tuple_with_three_callables():
    client = ResilientYouTubeClient(keys=["k1"])

    callables = client.make_callables()

    assert isinstance(callables, YouTubeCallables)
    assert callable(callables.list_videos)
    assert callable(callables.list_channels)
    assert callable(callables.list_categories)


def test_normal_path_single_key_returns_response():
    """Key 1개, 정상 응답 — 가장 단순한 성공 경로."""
    calls = []

    def fake_service_factory(key):
        def list_videos(**kw):
            calls.append(("videos", key, kw))
            return _fake_videos_response()

        def list_channels(**kw):
            calls.append(("channels", key, kw))
            return {"items": []}

        def list_categories(**kw):
            calls.append(("categories", key, kw))
            return {"items": []}

        return list_videos, list_channels, list_categories

    client = ResilientYouTubeClient(
        keys=["k1"], _service_factory=fake_service_factory
    )
    callables = client.make_callables()

    result = callables.list_videos(part="snippet", chart="mostPopular")

    assert result == _fake_videos_response()
    assert len(calls) == 1
    assert calls[0][1] == "k1"  # Key 1개 사용
