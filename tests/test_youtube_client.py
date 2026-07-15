import json
import logging
import socket
import ssl
import traceback

import pytest
import requests

from autoresearch.youtube_collection.client import (
    CollectionExhausted,
    ResilientYouTubeClient,
    Verdict,
    YouTubeCallables,
    _classify_error,
    _parse_reason_from_content,
    _RetryableHttpError,
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
        (400, "", Verdict.BACKOFF),  # unknown 4xx → 일시적 기본 정책
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

    client = ResilientYouTubeClient(keys=["k1"], _service_factory=fake_service_factory)
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


def test_ip_ban_signature_proxy_none_short_circuits():
    """전 Key 동일 403(기타 reason) + proxy_url=None → 즉시 CollectionExhausted.

    시그니처 성립(Key≥2, 전 Key 동일 403 IP_BAN_CANDIDATE).
    """

    def factory(key):
        def list_videos(**kw):
            raise FakeHttpError(403, "suspended")  # 기타 403 → IP_BAN_CANDIDATE

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1", "k2"], proxy_url=None, _service_factory=factory
    )

    with pytest.raises(CollectionExhausted, match="IP 밴"):
        client.make_callables().list_videos(part="snippet")


def test_ip_ban_signature_single_key_does_not_qualify():
    """Key 1개 + 403 → 시그니처 미성립(최소 Key≥2). CollectionExhausted(rotate 소진)."""

    def factory(key):
        def list_videos(**kw):
            raise FakeHttpError(403, "suspended")

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1"], proxy_url=None, _service_factory=factory
    )

    with pytest.raises(CollectionExhausted) as exc_info:
        client.make_callables().list_videos(part="snippet")
    # IP 밴 메시지가 아님 — 시그니처 미성립으로 회전 소진 처리.
    assert "IP 밴" not in str(exc_info.value)


def test_ip_ban_signature_partial_success_does_not_qualify():
    """k1=403 suspended, k2=200 → 부분 성공, 시그니처 불성립 → k2 응답 반환."""

    def factory(key):
        def list_videos(**kw):
            if key == "k1":
                raise FakeHttpError(403, "suspended")
            return _fake_videos_response()

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1", "k2"], proxy_url=None, _service_factory=factory
    )
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()


def test_ip_ban_signature_uses_proxy_when_configured(monkeypatch):
    """전 Key 동일 403 + proxy_url 있음 → breaker_open 마킹 후 프록시 경로 진입.

    시그니처 감지 → Circuit Breaker OPEN → _call_via_proxy 로 전환.
    requests.get 을 가짜 응답(200)으로 격리하여 네트워크 호출 0건을 보장한다.
    """

    def factory(key):
        def list_videos(**kw):
            raise FakeHttpError(403, "suspended")

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1", "k2"],
        proxy_url="https://fake-proxy.example.com",
        _service_factory=factory,
    )

    class FakeResp:
        status_code = 200

        def json(self):
            return {"items": [{"id": "v1"}]}

        text = ""

    # _call_via_proxy 의 requests.get 을 가짜 응답으로 대체(네트워크 격리).
    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResp())

    result = client.make_callables().list_videos(part="snippet")
    assert result == {"items": [{"id": "v1"}]}
    assert client._breaker_open is True  # 시그니처 확정 마킹


def test_max_total_calls_guards_against_runaway():
    """max_total_calls=3 초과 → CollectionExhausted (무효 Key 반복 루프 방지)."""

    def factory(key):
        def list_videos(**kw):
            return _fake_videos_response()  # 정상이어도 호출 수 누적

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(
        keys=["k1"], max_total_calls=3, _service_factory=factory
    )
    callables = client.make_callables()
    callables.list_videos(part="a")
    callables.list_channels(part="b")
    callables.list_categories(part="c")

    with pytest.raises(CollectionExhausted, match="폭주 가드"):
        callables.list_videos(part="d")  # 4회째


def test_403_reason_none_rotates_to_next_key():
    """403 + reason=None(본문 파싱 불가, CDN/IP밴 에러페이지) → IP_BAN_CANDIDATE
    이지만 시그니처 미카운트. key1 회전 마킹(sentinel) → key2 로 넘어감.

    같은 key 로 반복 시도하지 않음(타이트 루프 방지, 설계 §5.2 엣지).
    """
    calls = []

    def factory(key):
        def list_videos(**kw):
            calls.append(key)
            if key == "k1":
                raise _RetryableHttpError(403, None)
            return _fake_videos_response()

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)
    result = client.make_callables().list_videos(part="snippet")

    assert result == _fake_videos_response()
    assert calls == ["k1", "k2"]


def test_403_reason_none_both_keys_exhausts_without_loop():
    """전 Key 403 reason=None → 시그니처 미성립 → 회전 소진 → CollectionExhausted.

    각 key 는 1회씩만 호출(tight loop 없이 call_budget 도달 전 소진).
    """
    calls = []

    def factory(key):
        def list_videos(**kw):
            calls.append(key)
            raise _RetryableHttpError(403, None)

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1", "k2"], _service_factory=factory)

    with pytest.raises(CollectionExhausted):
        client.make_callables().list_videos(part="snippet")

    assert calls.count("k1") == 1
    assert calls.count("k2") == 1


def test_record_ip_ban_candidate_reason_none_does_not_count_signature():
    """reason=None 후보는 시그니처 판정에서 제외(설계 §5.2 엣지 규칙).

    단 회전 추적용 sentinel("") 으로 마킹하여 _pick_active_key 가 스킵하게 함.
    """
    client = ResilientYouTubeClient(keys=["k1", "k2"])
    assert client._record_ip_ban_candidate("k1", "videos", None) is None
    assert client._record_ip_ban_candidate("k2", "videos", None) is None
    assert client._ip_ban_candidates == {"k1": "", "k2": ""}


def test_service_factory_cached_per_key():
    """동일 key 로 여러 호출 시 factory 는 key 당 1회만 호출.

    프로덕션 _default_service_factory 는 googleapiclient.build (discovery fetch)
    을 수반하므로 매 호출마다 재생성하면 비용이 크다.
    """
    factory_calls = []

    def fake_service_factory(key):
        factory_calls.append(key)

        def list_videos(**kw):
            return _fake_videos_response()

        return list_videos, lambda **kw: {"items": []}, lambda **kw: {"items": []}

    client = ResilientYouTubeClient(keys=["k1"], _service_factory=fake_service_factory)
    callables = client.make_callables()
    callables.list_videos(part="a")
    callables.list_channels(part="b")
    callables.list_categories(part="c")

    assert len(factory_calls) == 1


@pytest.mark.parametrize(
    "exc",
    [
        ssl.SSLEOFError("ssl eof"),
        BrokenPipeError("broken pipe"),
        InterruptedError("interrupted"),
        TimeoutError("timed out"),
    ],
)
def test_try_wrap_http_error_wraps_extended_network_errors(exc):
    """SSLEOFError/BrokenPipeError/InterruptedError/TimeoutError → 599 래핑(사각지대 보강)."""
    wrapped = _try_wrap_http_error(exc)
    assert wrapped is not None
    assert wrapped.status == 599
    assert wrapped.reason is None


def test_call_via_proxy_forwards_request_and_returns_json(monkeypatch):
    """_call_via_proxy: proxy_url/youtube/v3/<resource> 로 GET, X-Goog-Api-Key 헤더."""
    captured = {}

    class FakeResp:
        status_code = 200
        def json(self):
            return {"items": [{"id": "v1"}]}
        text = ""

    def fake_get(url, *, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return FakeResp()

    client = ResilientYouTubeClient(
        keys=["k1"],
        proxy_url="https://proxy.example.com",
    )
    # 정상 경로 service factory 가 호출되지 않도록 factory stub
    monkeypatch.setattr(client, "_service_factory", lambda key: (_ for _ in ()).throw(RuntimeError("should not call direct service")))
    monkeypatch.setattr("requests.get", fake_get)

    result = client._call_via_proxy("videos", {"part": "snippet"})
    assert result == {"items": [{"id": "v1"}]}
    assert captured["url"] == "https://proxy.example.com/youtube/v3/videos"
    assert captured["params"] == {"part": "snippet"}
    assert captured["headers"]["X-Goog-Api-Key"] == "k1"


def test_call_via_proxy_masks_credentials_in_proxy_url(monkeypatch):
    """proxy_url 임베디드 credentials 는 예외 메시지에 노출 안 함(호스트만)."""
    def fake_get(url, *, params=None, headers=None, timeout=None):
        raise requests.exceptions.HTTPError("upstream 503")

    client = ResilientYouTubeClient(
        keys=["k1"],
        proxy_url="https://user:__VG_EMAIL_x__@proxy.example.com@proxy.example.com:8080",
    )
    monkeypatch.setattr("requests.get", fake_get)

    with pytest.raises(CollectionExhausted) as exc_info:
        client._call_via_proxy("videos", {})
    msg = str(exc_info.value)
    assert "proxy.example.com" in msg
    assert "@" not in msg


def test_call_via_proxy_does_not_chain_raw_requests_exception(monkeypatch, caplog):
    """RequestException 은 __cause__ 로 체인 금지.

    raw requests 예외(예: ConnectionError)는 repr 에 proxy URL(credentials
    포함 가능)을 embed 한다. 이를 CollectionExhausted.__cause__ 로 보존하면
    logging.exception / traceback 출력 시 credentials 가 노출된다.
    따라서 __cause__ 는 None 이어야 하며, 대신 예외 타입명만 warning 로그에
    남겨 디버깅 정보를 보존한다.

    항목 3 변경(#152): RequestException 시 즉시 포기하지 않고 다음 attempt 로
    재시도. 모든 attempt 소진 시 "프록시 경로 소진" CollectionExhausted raise.
    디버깅 정보(err 타입명)는 warning 로그로만 보존된다.
    """
    credential = "__VG_EMAIL_x__@proxy.example.com"

    def fake_get(url, *, params=None, headers=None, timeout=None):
        raise requests.exceptions.ConnectionError(
            "HTTPSConnectionPool(host='proxy.example.com', port=8080): "
            "Max retries exceeded with url: /youtube/v3/videos "
            f"(Caused by connection to https://user:{credential}:8080)"
        )

    client = ResilientYouTubeClient(
        keys=["k1"],
        proxy_url=f"https://user:{credential}@proxy.example.com:8080",
    )
    monkeypatch.setattr("requests.get", fake_get)

    with caplog.at_level(
        logging.WARNING, logger="autoresearch.youtube_collection.client"
    ):
        with pytest.raises(CollectionExhausted) as exc_info:
            client._call_via_proxy("videos", {})
    exc = exc_info.value

    assert exc.__cause__ is None
    assert credential not in str(exc)
    assert "@" not in str(exc)
    tb_text = "".join(traceback.format_exception(exc))
    assert credential not in tb_text
    # 디버깅 정보(err 타입명)는 warning 로그로만 보존 — credentials 노출 없이.
    assert any(
        "ConnectionError" in r.getMessage() and credential not in r.getMessage()
        for r in caplog.records
    )


def test_call_via_proxy_429_raises_collection_exhausted(monkeypatch):
    """upstream 429 → CollectionExhausted(회전/재시도 외곽에서 처리)."""
    class FakeResp:
        status_code = 429
        text = '{"error":{"errors":[{"reason":"quotaExceeded"}],"code":429}}'
        def json(self):
            import json
            return json.loads(self.text)

    client = ResilientYouTubeClient(keys=["k1"], proxy_url="https://proxy.example.com")
    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResp())

    with pytest.raises(CollectionExhausted):
        client._call_via_proxy("videos", {})


class _ProxyResp:
    """_call_via_proxy 테스트용 응답. .content 를 통해 reason 파싱이 가능해야 한다."""

    def __init__(self, status_code, body_json):
        self.status_code = status_code
        self.content = body_json.encode()

    def json(self):
        return json.loads(self.content)


def test_call_via_proxy_rotates_on_key_invalid(monkeypatch):
    """proxy 루프에서 keyInvalid(ROTATE) 시 다음 Key 회전(normal route 와 일관).

    k1 → 400 keyInvalid → 무효화 → k2 → 200 성공. fix 전에는 verdict 미처리로
    k1 만 반복 재시도 후 max_proxy_attempts 소진 → CollectionExhausted 였음.
    """
    used_keys = []

    def fake_get(url, *, params=None, headers=None, timeout=None):
        key = headers["X-Goog-Api-Key"]
        used_keys.append(key)
        if key == "k1":
            return _ProxyResp(
                400, '{"error":{"errors":[{"reason":"keyInvalid"}],"code":400}}'
            )
        return _ProxyResp(200, '{"items":[]}')

    client = ResilientYouTubeClient(
        keys=["k1", "k2"],
        proxy_url="https://proxy.example.com",
    )
    monkeypatch.setattr("requests.get", fake_get)

    result = client._call_via_proxy("videos", {"part": "snippet"})
    assert result == {"items": []}
    assert used_keys == ["k1", "k2"]
    assert "k1" in client._invalid_keys


def test_call_via_proxy_terminal_quota_raises_immediately(monkeypatch):
    """proxy 루프에서 dailyLimitExceeded(TERMINAL_QUOTA) 시 즉시 CollectionExhausted.

    회전 무효(전 Key 동일). 남은 proxy attempt 를 소진하지 않고 즉시 승격한다.
    """
    call_count = 0

    def fake_get(url, *, params=None, headers=None, timeout=None):
        nonlocal call_count
        call_count += 1
        return _ProxyResp(
            403, '{"error":{"errors":[{"reason":"dailyLimitExceeded"}],"code":403}}'
        )

    client = ResilientYouTubeClient(
        keys=["k1", "k2"],
        proxy_url="https://proxy.example.com",
    )
    monkeypatch.setattr("requests.get", fake_get)

    with pytest.raises(CollectionExhausted):
        client._call_via_proxy("videos", {})
    assert call_count == 1  # 즉시 승격, 남은 attempt 미사용


def test_call_via_proxy_200_non_json_raises_collection_exhausted(monkeypatch):
    """200 + non-JSON 본문 → CollectionExhausted(skip+알림), raw JSONDecodeError 전파 금지.

    fix 전: resp.json() 의 JSONDecodeError 가 _RetryableHttpError 가 아니어서
    회전/tenacity 에서 잡히지 않아 회전 루프 밖으로 전파 → DAG 크래시(skip+알림 아님).
    """
    class NonJsonResp:
        status_code = 200
        content = b"<html>Not JSON</html>"

        def json(self):
            raise json.JSONDecodeError("Expecting value", "<html>", 0)

    client = ResilientYouTubeClient(
        keys=["k1"],
        proxy_url="https://proxy.example.com",
    )
    monkeypatch.setattr("requests.get", lambda *a, **k: NonJsonResp())

    with pytest.raises(CollectionExhausted):
        client._call_via_proxy("videos", {})


def test_success_path_logs_ok(caplog):
    """정상 경로 성공 시 info 로그 기록(관측성, key_index 만)."""
    factory = _make_service_that_raises(then_return=_fake_videos_response())
    client = ResilientYouTubeClient(keys=["k1"], _service_factory=factory)

    with caplog.at_level(logging.INFO, logger="autoresearch.youtube_collection.client"):
        client.make_callables().list_videos(part="snippet")

    assert any(
        "youtube call ok" in r.getMessage() and "key_index=0" in r.getMessage()
        for r in caplog.records
    )


def test_ip_ban_signature_switches_to_proxy(monkeypatch):
    """전 Key 동일 403 + proxy_url 설정 → _call_via_proxy 로 전환 성공."""
    factory = _make_service_that_raises(
        _RetryableHttpError(403, "suspended", None),
        _RetryableHttpError(403, "suspended", None),
    )
    client = ResilientYouTubeClient(
        keys=["k1", "k2"],
        proxy_url="https://proxy.example.com",
        _service_factory=factory,
    )

    class FakeResp:
        status_code = 200
        def json(self):
            return {"items": [{"id": "v1"}]}
        text = ""

    monkeypatch.setattr("requests.get", lambda *a, **k: FakeResp())

    result = client._call_with_resilience("videos", {"part": "snippet"})
    assert result == {"items": [{"id": "v1"}]}
    assert client._breaker_open is True  # 시그니처 확정 마킹
