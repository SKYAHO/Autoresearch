import pytest
from fastapi.testclient import TestClient
from proxy.app import app

client = TestClient(app)


@pytest.fixture(autouse=True)
def _reset_proxy_state():
    """proxy app.state 격리: 각 테스트 직전 unhealthy/streak 을 clean state 로 리셋.

    TestClient 는 모듈 수명 동안 하나의 app 인스턴스를 공유한다. 한 테스트가
    unhealthy=True / _unhealthy_streak>=3 으로 마킹하면 후속 테스트의 /health 가
    오염된다. autouse 로 매 테스트마다 clean state 보장(pytest-randomly 및 임의
    실행 순서, 테스트 추가 시에도 격리 유지).
    """
    app.state.unhealthy = False
    app.state._unhealthy_streak = 0
    yield


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


def test_upstream_success_resets_streak_but_keeps_unhealthy(monkeypatch):
    """정상(200) 응답 시 streak 리셋. unhealthy 플래그는 non-goal(once-True-stays-True).

    설계 의도: unhealthy=True 가 한 번 붙으면 Cloud Run 재시작으로만 헬스 복구를
    유도하기 위해 200 수신해도 플래그는 되돌리지 않는다(IP밴 장기화 회피 목적의
    의도적 non-goal). 본 테스트는 (1) streak 리셋(실제 goal) 과 (2) unhealthy
    유지(non-goal 명시) 를 동시에 검증하여, 향후 "테스트 통과 목적"으로 200 분기에
    unhealthy=False 를 끼워넣는 회귀를 잡는다.

    주의: 이전 버전 test_upstream_success_clears_unhealthy 는 진입 시점에
    app.state.unhealthy=False 를 수동 세팅한 뒤 is False 를 단정해 tautology 였음
    (프덕 코드가 리셋한 게 아니라 테스트가 세팅한 값 확인). 본 테스트는
    unhealthy=True 로 preset 한 뒤 200 을 받아도 True 가 유지됨을 검증.
    """
    # given: 이미 unhealthy 마킹된 상태에서 200 수신
    app.state.unhealthy = True
    app.state._unhealthy_streak = 3
    monkeypatch.setattr(
        "proxy.app._upstream_get",
        lambda *a, **k: _FakeResp(200, {"items": []}),
    )
    # when
    client.get("/youtube/v3/videos", headers={"X-Goog-Api-Key": "k1"})
    # then: streak 은 리셋(goal), unhealthy 플래그는 유지(non-goal: once-True-stays-True)
    assert app.state._unhealthy_streak == 0
    assert app.state.unhealthy is True


class _FakeResp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = ""

    def json(self):
        return self._payload
