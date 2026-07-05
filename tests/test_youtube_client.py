import json
import socket
import ssl

import pytest
import requests

from autoresearch.youtube_collection.client import (
    CollectionExhausted,
    ResilientYouTubeClient,
    Verdict,
    YouTubeCallables,
    _classify_error,
    _parse_reason_from_content,
    _try_wrap_http_error,
)


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


class FakeHttpError(Exception):
    """googleapiclient.errors.HttpError 흉내. status/reason 전달용."""

    def __init__(self, status: int, reason: str | None):
        self.status = status
        self.reason = reason
        body = {"error": {"errors": [{"reason": reason or ""}]}}
        # googleapiclient HttpError 는 resp.status 와 content(JSON bytes)를 가짐.

        class _Resp:
            def __init__(self, s):
                self.status = s

        self.resp = _Resp(status)
        self.content = json.dumps(body).encode()
        super().__init__(f"FakeHttpError status={status} reason={reason}")


def _make_service_that_raises(*errors, then_return=None):
    """errors 순서대로 raise 하다가, 소진 후 then_return 반환하는 service 팩토리."""
    state = {"i": 0}

    def factory(key):
        def list_videos(**kw):
            i = state["i"]
            if i < len(errors):
                state["i"] += 1
                raise errors[i]
            return then_return or _fake_videos_response()

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    return factory


def test_5xx_backoff_then_success():
    """500 → 503 → 200. tenacity backoff 후 정상 복귀."""
    factory = _make_service_that_raises(
        FakeHttpError(500, "internalError"),
        FakeHttpError(503, "backendError"),
        then_return=_fake_videos_response(),
    )
    client = ResilientYouTubeClient(
        keys=["k1"], max_retries=3, _service_factory=factory
    )
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()


def test_5xx_backoff_exhausted_raises_collection_exhausted():
    """500 × max_retries회 반복 → 소진 → CollectionExhausted."""
    factory = _make_service_that_raises(
        FakeHttpError(500, "internalError"),
        FakeHttpError(500, "internalError"),
        FakeHttpError(500, "internalError"),
    )
    client = ResilientYouTubeClient(
        keys=["k1"], max_retries=3, _service_factory=factory
    )

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")


def test_ratelimit_backoff_then_success():
    """rateLimitExceeded → 200. BACKOFF reason 도 backoff 후 복귀."""
    factory = _make_service_that_raises(
        FakeHttpError(403, "rateLimitExceeded"),
        then_return=_fake_videos_response(),
    )
    client = ResilientYouTubeClient(
        keys=["k1"], max_retries=3, _service_factory=factory
    )
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()


def test_try_wrap_http_error_wraps_network_errors_to_599():
    """DNS/SSL/Timeout/Connection 예외는 599 가상 코드로 래핑(reason None)."""
    network_errors = [
        socket.gaierror("dns fail"),
        ssl.SSLError("ssl fail"),
        requests.exceptions.Timeout("timeout"),
        requests.exceptions.ConnectionError("conn"),
    ]
    for exc in network_errors:
        wrapped = _try_wrap_http_error(exc)
        assert wrapped is not None
        assert wrapped.status == 599
        assert wrapped.reason is None


def test_parse_reason_from_content_malformed_returns_none():
    """JSON 파싱 실패/빈 본문/누락 → reason None."""
    assert _parse_reason_from_content(b"not json") is None
    assert _parse_reason_from_content(b"") is None
    assert _parse_reason_from_content(None) is None
    assert _parse_reason_from_content(b'{"error": {}}') is None


def test_key_invalid_rotates_to_next_key_and_succeeds():
    """k1 → 400 keyInvalid → k2 → 200. Key 무효화 마킹 + 회전 성공."""
    state = {"k1_calls": 0}

    def factory(key):
        def list_videos(**kw):
            if key == "k1":
                state["k1_calls"] += 1
                raise FakeHttpError(400, "keyInvalid")
            return _fake_videos_response()  # k2 정상

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1", "k2"], max_retries=2, _service_factory=factory
    )
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()
    assert state["k1_calls"] == 1  # 1회만 호출, tenacity 반복 X (ROTATE는 즉시)


def test_key_expired_treated_same_as_key_invalid():
    """400 keyExpired → 회전."""
    def factory(key):
        def list_videos(**kw):
            if key == "k1":
                raise FakeHttpError(400, "keyExpired")
            return _fake_videos_response()

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()


def test_401_auth_rotates_to_next_key():
    """401 unauthorized → 회전."""
    def factory(key):
        def list_videos(**kw):
            if key == "k1":
                raise FakeHttpError(401, "unauthorized")
            return _fake_videos_response()

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()


def test_all_keys_invalid_raises_collection_exhausted():
    """k1, k2 모두 keyInvalid → CollectionExhausted."""
    def factory(key):
        def list_videos(**kw):
            raise FakeHttpError(400, "keyInvalid")

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")


def test_quota_exceeded_skips_without_rotation():
    """403 quotaExceeded → 회전 없이 즉시 CollectionExhausted(프로젝트 단위)."""
    call_count = {"n": 0}

    def factory(key):
        def list_videos(**kw):
            call_count["n"] += 1
            raise FakeHttpError(403, "quotaExceeded")
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")
    # 회전 안 했음 — k1 1회 호출 후 즉시 터미널.
    assert call_count["n"] == 1


def test_access_not_configured_skips_without_rotation():
    """403 accessNotConfigured → 회전 없이 즉시 CollectionExhausted(프로젝트 스코프)."""
    call_count = {"n": 0}

    def factory(key):
        def list_videos(**kw):
            call_count["n"] += 1
            raise FakeHttpError(403, "accessNotConfigured")
        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")
    assert call_count["n"] == 1  # k2 로 회전 안 함
