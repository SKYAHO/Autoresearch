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


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload
