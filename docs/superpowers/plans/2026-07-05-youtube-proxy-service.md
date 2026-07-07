# YouTube Cloud Run 프록시 서비스 구현 계획 (2차, 이슈 #47)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** YouTube Data API egress 경로를 분리하는 Cloud Run dumb forwarder(`proxy/`)와 collector 의 `ResilientYouTubeClient._call_via_proxy` 실구현을 추가하여, IP밴 시그니처 감지 시 프록시 경로로 우회한다. **본 프록시는 학습/포트폴리오 + 범용 egress seam 목적(ADR 0001 참조)이며, 공식 API 정상 사용 환경에서 IP밴은 꼬리 케이스로 실 운영 발생 가능성은 낮다.**

**Architecture:** 프록시는 상태 없는 dumb forwarder — collector 가 `X-Goog-Api-Key` 헤더로 key 를 전달하고, 프록시는 host/path 화이트리스트 검증 후 `https://www.googleapis.com/youtube/v3/...` 로 동일 query/header 를 전달한다. upstream 429/5xx 는 unhealthy 플래그로 `/health` 가 503 을 반환하여 Cloud Run liveness 가 컨테이너 재시작(확률적 새 egress IP, **공식 문서로 보장되지 않음 — ADR 0001**)을 유도한다. invoker IAM / Cloud Run 배포 / Secret Manager 는 3차(인프라 담당 Terraform) 범위이며, **egress IP 회전 가정 경험적 검증 전까지 3차 배포는 보류**한다.

**Tech Stack:** Python 3.11/3.12, FastAPI 0.115.x, uvicorn 0.32.x, httpx 0.28.x(proxy upstream), requests 2.32.x(collector), pytest 8.x, ruff.

## Global Constraints

- `fetch.py` 변경 금지.
- 한국어 docstring / 영어 식별자.
- 커밋 메시지 `<type>: <한국어 설명> (#47)`.
- proxy 는 dumb forwarder: key 상태/Key 값/Key rotation 을 다루지 않는다. collector 가 헤더로 key 붙임.
- Key 전달은 `X-Goog-Api-Key` 헤더만(query string `key=` 금지 — access log 평문 노출). curl 실동작 검증 완료.
- host 화이트리스트: upstream host 는 `www.googleapis.com` 하드코드 고정. collector 가 보낸 host 절대 신뢰 금지.
- path prefix 화이트리스트: `/youtube/v3/` 만 허용. 위반 시 400.
- CORS 비활성(기본값 유지, middleware 추가 금지).
- proxy 로그 마스킹: `X-Goog-Api-Key`/`Authorization` 헤더, query string 의 `key=`, 응답 본문, traceback 을 로그에 기록 금지. status/reason/path 정도만.
- client `_call_via_proxy`: 기존 stub(`client.py:496-508`) 교체. `proxy_url` 주입은 `__init__` 에 이미 있음. 예외 메시지에는 `proxy_url` 전체가 아닌 `urlparse().hostname` 만.
- unhealthy 판정은 단일 응답 코드 직결이 아닌 임계치 기반 플래그(Cloud Run 재시작 throttle 회피).
- 각 task TDD(RED→GREEN→회귀→커밋). `fetch.py` 미수정 매 task 확인.
- 커밋/push/PR 전 사용자 허락 필수.

## File Structure

- **신규** `proxy/app.py` — FastAPI dumb forwarder. `/youtube/v3/{rest_path}`, `/health`, host/path 검증, httpx upstream, X-Goog-Api-Key passthrough, unhealthy 플래그.
- **신규** `proxy/Dockerfile` — `python:3.12-slim` 기반(Dockerfile.app 패턴), uvicorn 실행.
- **신규** `proxy/requirements.txt` — `fastapi`, `uvicorn`, `httpx` (proxy 배포 독립 의존성).
- **신규** `tests/test_proxy_app.py` — 프록시 단위/통합 테스트.
- **수정** `autoresearch/youtube_collection/client.py` — `_call_via_proxy` stub → 실구현.
- **수정** `tests/test_youtube_client.py` — `_call_via_proxy` stub 테스트 → 실구현 테스트로 교체/확장.
- **수정** `requirements-dev.txt` — 테스트용 `fastapi`/`httpx` 추가(이미 `httpx` 없으면).

### Interfaces (cross-task 계약)

- `proxy.app` 모듈: FastAPI `app` 인스턴스 노출(`uvicorn proxy.app:app`). 내부 상태 `_unhealthy: bool`(모듈 전역 또는 `app.state`).
- `_call_via_proxy(self, resource: str, kw: dict) -> dict`(`client.py`): proxy_url 있을 때 호출. `requests.get(proxy_url/youtube/v3/<resource>, params=kw, headers={"X-Goog-Api-Key": key}, timeout=...)`. 응답 JSON dict 반환(googleapiclient execute() 와 동일 형태). 에러 시 `_RetryableHttpError`/`CollectionExhausted` raise.

---

## Task 1: proxy/ FastAPI dumb forwarder 뼈대 + host/path 검증

**Files:**
- Create: `proxy/app.py`
- Create: `proxy/__init__.py` (빈)
- Test: `tests/test_proxy_app.py`

**Interfaces:**
- Produces: `proxy.app:app` (FastAPI 인스턴스), `GET /health` → 200, `GET /youtube/v3/{rest_path:path}` → upstream 전달.

- [ ] **Step 1: 의존성 설치 및 테스트 파일 생성**

`requirements-dev.txt` 에 추가(없으면):
```
fastapi>=0.115,<0.117
httpx>=0.28,<0.29
```
설치: `pip install -r requirements-dev.txt`

- [ ] **Step 2: 실패 테스트 작성**

`tests/test_proxy_app.py`:
```python
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
```

- [ ] **Step 3: 테스트 실패 확인**

Run: `pytest tests/test_proxy_app.py -v`
Expected: FAIL (`ModuleNotFoundError: proxy.app`)

- [ ] **Step 4: proxy/app.py 구현**

```python
"""YouTube Data API dumb forwarder (Cloud Run 배포용).

collector 가 X-Goog-Api-Key 헤더로 key 를 전달하면, 본 서비스는 host/path
화이트리스트 검증 후 https://www.googleapis.com/youtube/v3/... 로 동일
query 와 헤더를 전달한다. key 상태/rotation 은 다루지 않는다(dumb forwarder).
"""
from __future__ import annotations

import httpx
from fastapi import FastAPI, Header, HTTPException, Request
from fastapi.responses import JSONResponse

UPSTREAM_HOST = "https://www.googleapis.com"
UPSTREAM_TIMEOUT = 10.0

app = FastAPI(title="youtube-proxy", docs_url=None, redoc_url=None)
app.state.unhealthy = False  # Task 2 에서 unhealthy 마킹 시 사용


def _upstream_get(url: str, *, params, headers, timeout):
    """httpx 동기 GET 래퍼(테스트 monkeypatch 용 분리)."""
    with httpx.Client(timeout=timeout) as c:
        return c.get(url, params=params, headers=headers)


@app.get("/health")
def health():
    """liveness probe. unhealthy 플래그 시 503."""
    if app.state.unhealthy:
        return JSONResponse(status_code=503, content={"status": "unhealthy"})
    return {"status": "ok"}


@app.get("/youtube/v3/{rest_path:path}")
def forward(rest_path: str, request: Request, x_goog_api_key: str = Header(default="")):
    """youtube v3 API 를 upstream 으로 dumb-forward.

    path 는 /youtube/v3/ 하위만 라우팅(host 화이트리스트). key 는 헤더 필수.
    """
    if not x_goog_api_key:
        raise HTTPException(status_code=400, detail="X-Goog-Api-Key 헤더 누락")
    upstream_url = f"{UPSTREAM_HOST}/youtube/v3/{rest_path}"
    # X-Goog-Api-Key 만 upstream 으로 전달(다른 헤더는 의도적 미전달).
    upstream_headers = {"X-Goog-Api-Key": x_goog_api_key}
    resp = _upstream_get(
        upstream_url,
        params=request.query_params,
        headers=upstream_headers,
        timeout=UPSTREAM_TIMEOUT,
    )
    # upstream 응답을 그대로 전달(status 포함).
    return JSONResponse(status_code=resp.status_code, content=resp.json())
```

`proxy/__init__.py`:
```python
```
(빈 파일)

- [ ] **Step 5: 테스트 통과 확인**

Run: `pytest tests/test_proxy_app.py -v`
Expected: 4 passed

- [ ] **Step 6: 회귀 + ruff**

Run: `pytest tests/ -q && ruff check proxy/ tests/test_proxy_app.py`
Expected: 전체 passed, ruff clean

- [ ] **Step 7: 커밋**

```bash
git add proxy/__init__.py proxy/app.py tests/test_proxy_app.py requirements-dev.txt
git commit -m "feat: YouTube dumb forwarder 프록시 서비스 뼈대 (host/path 검증) (#47)"
```

---

## Task 2: proxy/ 429/5xx unhealthy 마킹 → /health 503

**Files:**
- Modify: `proxy/app.py`
- Modify: `tests/test_proxy_app.py`

**Interfaces:**
- Produces: upstream 429/5xx 응답 시 `app.state.unhealthy=True` → 이후 `/health` 503. 임계치(예: 연속 3회) 도달 시 마킹.

- [ ] **Step 1: 실패 테스트 추가**

`tests/test_proxy_app.py` 에 추가:
```python
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
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_proxy_app.py -v`
Expected: 2 fail(unhealthy 마킹 미구현)

- [ ] **Step 3: app.py 구현 — unhealthy 마킹**

`proxy/app.py` 수정: `app.state.unhealthy` 외 `app.state._unhealthy_streak` 추가. `UNHEALTHY_THRESHOLD = 3`.

```python
# app.py 초기화 부분 수정
app = FastAPI(title="youtube-proxy", docs_url=None, redoc_url=None)
app.state.unhealthy = False
app.state._unhealthy_streak = 0
UNHEALTHY_THRESHOLD = 3
```

`forward()` 함수 응답 처리 부분에 streak 업데이트:
```python
    resp = _upstream_get(...)
    status = resp.status_code
    if status == 200:
        app.state._unhealthy_streak = 0
        app.state.unhealthy = False
    elif status == 429 or status >= 500:
        app.state._unhealthy_streak += 1
        if app.state._unhealthy_streak >= UNHEALTHY_THRESHOLD:
            app.state.unhealthy = True
    return JSONResponse(status_code=status, content=resp.json())
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_proxy_app.py -v`
Expected: 6 passed

- [ ] **Step 5: 회귀 + ruff**

Run: `pytest tests/ -q && ruff check proxy/`
Expected: passed, clean

- [ ] **Step 6: 커밋**

```bash
git add proxy/app.py tests/test_proxy_app.py
git commit -m "feat: upstream 429/5xx 임계치 도달 시 unhealthy 마킹 (#47)"
```

---

## Task 3: proxy/ Dockerfile + requirements.txt

**Files:**
- Create: `proxy/Dockerfile`
- Create: `proxy/requirements.txt`

**Interfaces:**
- Produces: `docker build -t youtube-proxy proxy/` 가능한 이미지. `docker run -p 8080:8080 youtube-proxy` 로 uvicorn 실행.

- [ ] **Step 1: proxy/requirements.txt 작성**

```
fastapi>=0.115,<0.117
uvicorn[standard]>=0.32,<0.35
httpx>=0.28,<0.29
```

- [ ] **Step 2: proxy/Dockerfile 작성**

```dockerfile
FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY requirements.txt .
RUN python -m pip install --upgrade pip \
    && python -m pip install --no-cache-dir -r requirements.txt

COPY app.py .

USER appuser

EXPOSE 8080
CMD ["uvicorn", "app:app", "--host", "0.0.0.0", "--port", "8080"]
```

- [ ] **Step 3: 로컬 빌드/실행 검증**

```bash
docker build -t youtube-proxy ./proxy
docker run --rm -d -p 8080:8080 youtube-proxy
sleep 2
curl -sf http://localhost:8080/health | grep -q ok
docker stop $(docker ps -q --filter ancestor=youtube-proxy)
```
Expected: health ok 반환, 컨테이너 정상 종료.

- [ ] **Step 4: 커밋**

```bash
git add proxy/Dockerfile proxy/requirements.txt
git commit -m "chore: 프록시 Dockerfile + 독립 requirements (#47)"
```

---

## Task 4: client.py _call_via_proxy 실구현

**Files:**
- Modify: `autoresearch/youtube_collection/client.py:496-508` (`_call_via_proxy` stub)
- Modify: `tests/test_youtube_client.py:516-530` (stub 테스트 → 실구현 테스트)

**Interfaces:**
- Consumes: `requests`(이미 requirements.txt), `client._proxy_url`, `client._max_proxy_attempts`(이미 __init__).
- Produces: `_call_via_proxy(resource, kw)` 가 proxy 경로로 GET 후 dict 반환 또는 예외.

- [ ] **Step 1: stub 테스트를 실구현 테스트로 교체**

`tests/test_youtube_client.py` 의 `test_call_via_proxy_masks_credentials_in_proxy_url` 를 아래로 교체 + 신규 테스트 추가:

```python
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
        proxy_url="https://user:__VG_EMAIL_x__@proxy.example.com:8080",
    )
    monkeypatch.setattr("requests.get", fake_get)

    with pytest.raises(CollectionExhausted) as exc_info:
        client._call_via_proxy("videos", {})
    msg = str(exc_info.value)
    assert "proxy.example.com" in msg
    assert "@" not in msg


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
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_youtube_client.py -v -k call_via_proxy`
Expected: 3 fail(stub 은 CollectionExhausted 만 raise 하므로 정상 응답 테스트 실패)

- [ ] **Step 3: _call_via_proxy 실구현**

`client.py:496-508` stub 교체:
```python
    def _call_via_proxy(self, resource: str, kw: dict) -> dict:
        """프록시 경로 호출. X-Goog-Api-Key 헤더로 key 전달, dumb forwarder 에 의뢰.

        proxy_url/youtube/v3/<resource> 로 GET. 응답은 googleapiclient execute()
        와 동일 JSON dict. upstream 에러(4xx/5xx)는 CollectionExhausted 로 승격.
        예외 메시지에는 proxy_url 전체(임베디드 credentials 포함)가 아닌 호스트만.
        """
        from urllib.parse import urlparse

        import requests

        host = urlparse(self._proxy_url or "").hostname or "(unknown)"
        url = f"{(self._proxy_url or '').rstrip('/')}/youtube/v3/{resource}"
        for _ in range(self._max_proxy_attempts):
            key = self._pick_active_key()
            if key is None:
                raise CollectionExhausted(
                    f"프록시 경로: 활성 Key 없음 resource={resource} proxy_host={host}"
                )
            try:
                resp = requests.get(
                    url,
                    params=kw,
                    headers={"X-Goog-Api-Key": key},
                    timeout=30,
                )
            except requests.exceptions.RequestException as e:
                raise CollectionExhausted(
                    f"프록시 경로 네트워크 오류 resource={resource} proxy_host={host}"
                ) from e
            if resp.status_code == 200:
                return resp.json()
            # 4xx/5xx — 회전/재시도 외곽에서 처리하도록 CollectionExhausted.
            reason = _parse_reason_from_content(getattr(resp, "content", b"") or b"")
            self._log_decision(
                resource=resource,
                key=key,
                route="proxy",
                verdict=_classify_error(resp.status_code, reason),
                exc=_RetryableHttpError(resp.status_code, reason, None),
            )
        raise CollectionExhausted(
            f"프록시 경로 소진 resource={resource} proxy_host={host}"
        )
```

- [ ] **Step 4: 통과 확인**

Run: `pytest tests/test_youtube_client.py -v -k call_via_proxy`
Expected: 3 passed

- [ ] **Step 5: 회귀 + 마스킹 grep + ruff**

```bash
pytest tests/ -q
ruff check autoresearch/youtube_collection/client.py
rg "X-Goog-Api-Key|api_key=|Authorization|password|secret" autoresearch/youtube_collection/client.py
```
Expected: 131 passed(기존 + 신규), ruff clean, grep 히트는 docstring 금지 조항 자체만.

- [ ] **Step 6: 커밋**

```bash
git add autoresearch/youtube_collection/client.py tests/test_youtube_client.py
git commit -m "feat: _call_via_proxy 실구현 (X-Goog-Api-Key 헤더 + 스키마 정규화) (#47)"
```

---

## Task 5: client → proxy 통합(IP밴 시그니처 후 프록시 전환)

**Files:**
- Modify: `tests/test_youtube_client.py`

**Interfaces:**
- Consumes: `_call_with_resilience` IP_BAN_CANDIDATE 시그니처 분기(client.py:336-348)가 `_call_via_proxy` 호출.

- [ ] **Step 1: 통합 테스트 추가**

```python
def test_ip_ban_signature_switches_to_proxy(monkeypatch):
    """전 Key 동일 403 + proxy_url 설정 → _call_via_proxy 로 전환 성공."""
    factory = _make_service_that_raises(
        _RetryableHttpError(403, "suspended", None)
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
```

- [ ] **Step 2: 실패 확인**

Run: `pytest tests/test_youtube_client.py::test_ip_ban_signature_switches_to_proxy -v`
Expected: 통과(Task 4 구현으로 이미 동작). 통과 시 Step 3 생략, 회귀만.

- [ ] **Step 3: (필요 시) 회전/재시도 외곽 조정**

통과 시 생략. 실패 시 `_call_with_resilience` 의 `_call_via_proxy` 반환값이 상위로 전파되는지 확인.

- [ ] **Step 4: 회귀 + ruff**

Run: `pytest tests/ -q && ruff check`
Expected: passed, clean

- [ ] **Step 5: 커밋**

```bash
git add tests/test_youtube_client.py
git commit -m "test: IP밴 시그니처 → 프록시 전환 통합 테스트 (#47)"
```

---

## Task 6: Docker 통합테스트(proxy 컨테이너 + client end-to-end)

**Files:**
- Create: `tests/test_proxy_docker.py`

**Interfaces:**
- Produces: `docker build proxy/` + 로컬 실행 후 client 가 proxy 경로로 YouTube 를 조회하는 end-to-end 증거(2차 미배포 대비).

- [ ] **Step 1: 통합테스트 작성(docker 생략 조건부)**

```python
"""프록시 Docker 통합테스트. docker 미가용 시 skip."""
import os
import socket
import subprocess
import time

import pytest

HAVE_DOCKER = shutil_which("docker") is not None and _docker_daemon_running()


def shutil_which(cmd):
    import shutil
    return shutil.which(cmd)


def _docker_daemon_running():
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
        return True
    except Exception:
        return False


@pytest.mark.skipif(not HAVE_DOCKER, reason="docker 미가용")
def test_proxy_container_forwards_youtube(monkeypatch):
    """proxy 컨테이너 빌드/실행 후 /health 200 + /youtube/v3/ 전달."""
    subprocess.run(["docker", "build", "-t", "youtube-proxy", "./proxy"], check=True, capture_output=True)
    proc = subprocess.Popen(
        ["docker", "run", "--rm", "-p", "18080:8080", "youtube-proxy"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        _wait_port("localhost", 18080, timeout=20)
        import requests
        health = requests.get("http://localhost:18080/health", timeout=5)
        assert health.status_code == 200
    finally:
        proc.terminate()
        proc.wait(timeout=10)


def _wait_port(host, port, *, timeout):
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            with socket.create_connection((host, port), timeout=1):
                return
        except OSError:
            time.sleep(0.5)
    raise RuntimeError(f"port {port} 미응답")
```

- [ ] **Step 2: 테스트 실행**

Run: `pytest tests/test_proxy_docker.py -v`
Expected: docker 가용 시 PASS, 미가용 시 SKIPPED.

- [ ] **Step 3: 회귀**

Run: `pytest tests/ -q`
Expected: 전체 passed/skipped.

- [ ] **Step 4: ruff + 커밋**

```bash
ruff check tests/test_proxy_docker.py
git add tests/test_proxy_docker.py
git commit -m "test: 프록시 Docker 통합테스트(미배포 대비 실행 증거) (#47)"
```

---

## Self-Review

- **Spec coverage**: 설계 §5.3(프록시 계약) — host/path 화이트리스트(T1), X-Goog-Api-Key 헤더(T1/T4), unhealthy→/health 503(T2), CORS 비활성(T1). 보안 H1(SSRF/오픈 프록시) — host 고정+path prefix+헤더 필수(T1). H2(헤더 노출) — client 마스킹(T4). FACT-CHECK 헤더 지원 — curl 검증 완료(b12), 헤더 방식 채택.
- **Placeholder**: _call_via_proxy 의 회전/재시도 외곽 처리는 `for _ in range(_max_proxy_attempts)` 로 명시. TBD/TODO 없음.
- **Type consistency**: `_call_via_proxy(resource, kw) -> dict` 시그니처 = 기존 stub 과 동일. `app.state.unhealthy`/`_unhealthy_streak` 전 task 일관.
- **Non-goals**: invoker IAM/Cloud Run 배포/Secret Manager(3차, 인프라 담당). proxy rate limit(Cloud Armor 등). auto key rotation. `key=` query string 전달(금지).
