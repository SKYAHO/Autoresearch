from fastapi.testclient import TestClient
from proxy.app import app

client = TestClient(app)


def test_health_returns_200_when_healthy():
    response = client.get("/health")
    assert response.status_code == 200


def test_forwards_youtube_request_to_upstream(monkeypatch):
    """dumb forwarder: 동일 path+query 로 googleapis 전달, X-Goog-Api-Key passthrough."""
    captured = {}

    def fake_get(url, *, params=None, headers=None, timeout=None):
        captured["url"] = url
        captured["params"] = params
        captured["headers"] = headers
        return _FakeResp(200, {"items": []})

    monkeypatch.setattr("proxy.app._upstream_get", fake_get)
    response = client.get(
        "/youtube/v3/videos",
        params={"part": "snippet", "id": "vid1"},
        headers={"X-Goog-Api-Key": "k1"},
    )
    assert response.status_code == 200
    assert captured["url"] == "https://www.googleapis.com/youtube/v3/videos"
    assert captured["params"] == {"part": "snippet", "id": "vid1"}
    assert captured["headers"]["X-Goog-Api-Key"] == "k1"


def test_rejects_non_youtube_path():
    response = client.get("/compute/v1/instances")
    assert response.status_code == 404  # /youtube/v3/ 외 path 는 라우팅 안 됨


def test_rejects_missing_api_key_header():
    response = client.get("/youtube/v3/videos", params={"part": "snippet"})
    assert response.status_code == 400


def test_rejects_key_query_param(monkeypatch):
    """key= query param 은 마스킹 불변량 위반. 400 + upstream 미호출."""
    def fake_get(url, *, params=None, headers=None, timeout=None):
        raise AssertionError("upstream 이 호출되면 안 됨(key query param 차단 전)")

    monkeypatch.setattr("proxy.app._upstream_get", fake_get)
    response = client.get(
        "/youtube/v3/videos",
        params={"part": "snippet", "key": "SECRET"},
        headers={"X-Goog-Api-Key": "k1"},
    )
    assert response.status_code == 400
    assert "forbidden" in response.text


def test_upstream_429_marks_unhealthy(monkeypatch):
    """upstream 429 반복 시 unhealthy 마킹 → /health 503."""
    monkeypatch.setattr(
        "proxy.app._upstream_get",
        lambda *a, **k: _FakeResp(429, {"error": {"errors": [{"reason": "quotaExceeded"}], "code": 429}}),
    )
    # 임계치(3회) 도달 전에는 정상 응답 전달
    for _ in range(3):
        r = client.get("/youtube/v3/videos", headers={"X-Goog-Api-Key": "k1"})
        assert r.status_code == 429
    # 3회 후 unhealthy
    health = client.get("/health")
    assert health.status_code == 503


def test_upstream_success_clears_unhealthy(monkeypatch):
    """정상 응답 시 unhealthy 플래그 리셋."""
    app.state.unhealthy = False
    app.state._unhealthy_streak = 3  # 거의 임계
    monkeypatch.setattr(
        "proxy.app._upstream_get",
        lambda *a, **k: _FakeResp(200, {"items": []}),
    )
    client.get("/youtube/v3/videos", headers={"X-Goog-Api-Key": "k1"})
    assert app.state._unhealthy_streak == 0
    assert app.state.unhealthy is False


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload
