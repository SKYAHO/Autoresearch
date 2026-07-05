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
app.state._unhealthy_streak = 0
UNHEALTHY_THRESHOLD = 3


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
    # path traversal 방지: %2E%2E(URL 인코딩 ../) 는 Starlette 라우팅을 통과하므로
    # rest_path(이미 URL 디코딩됨) 에서 .. 세그먼트와 빈 세그먼트(//) 를 거부한다.
    # 위반 시 /youtube/v3/ 화이트리스트를 우회해 googleapis 임의 path 로 도달 가능.
    if ".." in rest_path.split("/") or any(seg == "" for seg in rest_path.split("/")):
        raise HTTPException(status_code=400, detail="path escape forbidden")
    # key= query param 은 마스킹 불변량 위반(URL 로그/캐시 에 key 노출).
    # 반드시 X-Goog-Api-Key 헤더로 전달. query 는 upstream forward 전 차단.
    # query param 키는 대소문자를 구분하지 않으므로 lower 비교로 우회(Key=/KEY=) 차단.
    if any(k.lower() == "key" for k in request.query_params):
        raise HTTPException(
            status_code=400,
            detail="key query param forbidden; use X-Goog-Api-Key header",
        )
    upstream_url = f"{UPSTREAM_HOST}/youtube/v3/{rest_path}"
    # X-Goog-Api-Key 만 upstream 으로 전달(다른 헤더는 의도적 미전달).
    upstream_headers = {"X-Goog-Api-Key": x_goog_api_key}
    try:
        resp = _upstream_get(
            upstream_url,
            params=dict(request.query_params),
            headers=upstream_headers,
            timeout=UPSTREAM_TIMEOUT,
        )
    except httpx.HTTPError:
        # upstream 네트워크 장애(Timeout/ConnectError 등) — streak 증가 + 502.
        # fix 전: 예외가 FastAPI 로 전파되어 500 + streak 미증가 → proxy 죽어도
        # /health 200 (설계 의도인 unhealthy→Cloud Run 재시작 무력화).
        app.state._unhealthy_streak += 1
        if app.state._unhealthy_streak >= UNHEALTHY_THRESHOLD:
            app.state.unhealthy = True
        return JSONResponse(status_code=502, content={"detail": "upstream unavailable"})
    status = resp.status_code
    if status == 200:
        app.state._unhealthy_streak = 0
    elif status == 429 or status >= 500:
        app.state._unhealthy_streak += 1
        if app.state._unhealthy_streak >= UNHEALTHY_THRESHOLD:
            app.state.unhealthy = True
    return JSONResponse(status_code=status, content=resp.json())
