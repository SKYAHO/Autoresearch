import httpx
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


def test_openapi_schema_disabled():
    """/openapi.json 노출 금지 — docs_url/redoc_url=None 만으로는 미차단.
    스키마(엔드포인트 목록) 누출 방지를 위해 openapi_url=None 필요.
    """
    response = client.get("/openapi.json")
    assert response.status_code == 404


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


@pytest.mark.parametrize("bad_param", ["Key", "KEY", "kEy"])
def test_rejects_key_query_param_case_variant(bad_param, monkeypatch):
    """대소문자 변형(Key=/KEY=) 도 차단 — HTTP header 와 달리 query param 키는
    case-insensitive 가 아니므로 명시적 lower 비교로 우회 차단."""
    def fake_get(url, *, params=None, headers=None, timeout=None):
        raise AssertionError("upstream 이 호출되면 안 됨(key 대소문자 우회 차단 전)")

    monkeypatch.setattr("proxy.app._upstream_get", fake_get)
    response = client.get(
        "/youtube/v3/videos",
        params={"part": "snippet", bad_param: "SECRET"},
        headers={"X-Goog-Api-Key": "k1"},
    )
    assert response.status_code == 400
    assert "forbidden" in response.text


def test_rejects_encoded_path_escape(monkeypatch):
    """%2E%2E(URL 인코딩 ../) 로 /youtube/v3/ 화이트리스트 우회 시도 → 400.

    리터럴 .. 는 Starlette 라우팅이 막지만 인코딩은 통과하므로, forward 단에서
    rest_path 검증이 필요하다. upstream 은 절대 호출되면 안 된다.
    """
    def fake_get(url, *, params=None, headers=None, timeout=None):
        raise AssertionError(f"upstream 이 호출되면 안 됨(path escape 차단 전): {url}")

    monkeypatch.setattr("proxy.app._upstream_get", fake_get)
    response = client.get(
        "/youtube/v3/%2E%2E/maps/api/place/json",
        headers={"X-Goog-Api-Key": "k1"},
    )
    assert response.status_code == 400


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


def test_upstream_exception_increments_streak_and_returns_502(monkeypatch):
    """_upstream_get 이 httpx 예외(ConnectError/Timeout) 시 streak 증가 + 502.

    fix 전: 예외가 FastAPI 로 전파되어 streak 미증가 → proxy 죽어도 /health 200
    (설계 의도인 unhealthy→재시작 무력화).
    """
    def fake_get(url, *, params, headers, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("proxy.app._upstream_get", fake_get)
    response = client.get("/youtube/v3/videos", headers={"X-Goog-Api-Key": "k1"})
    assert response.status_code == 502
    assert app.state._unhealthy_streak == 1


def test_upstream_exception_repeated_marks_unhealthy(monkeypatch):
    """httpx 예외 3회 누적 → unhealthy=True → /health 503."""
    def fake_get(url, *, params, headers, timeout):
        raise httpx.ConnectError("connection refused")

    monkeypatch.setattr("proxy.app._upstream_get", fake_get)
    for _ in range(3):
        r = client.get("/youtube/v3/videos", headers={"X-Goog-Api-Key": "k1"})
        assert r.status_code == 502
    assert app.state.unhealthy is True
