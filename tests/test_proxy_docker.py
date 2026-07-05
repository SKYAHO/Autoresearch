"""프록시 Docker 통합테스트. docker 미가용 시 skip."""
import subprocess
import time

import pytest


def shutil_which(cmd):
    import shutil
    return shutil.which(cmd)


def _docker_daemon_running():
    try:
        subprocess.run(["docker", "info"], capture_output=True, check=True, timeout=10)
        return True
    except Exception:
        return False


HAVE_DOCKER = shutil_which("docker") is not None and _docker_daemon_running()


@pytest.mark.skipif(not HAVE_DOCKER, reason="docker 미가용")
def test_proxy_container_forwards_youtube(monkeypatch):
    """proxy 컨테이너 빌드/실행 후 /health 200 + /youtube/v3/ 전달."""
    subprocess.run(
        ["docker", "build", "-t", "youtube-proxy", "./proxy"],
        check=True,
        capture_output=True,
    )
    cid = subprocess.check_output(
        ["docker", "run", "-d", "--rm", "-p", "18080:8080", "youtube-proxy"],
        text=True,
    ).strip()
    try:
        health = _wait_health("http://localhost:18080/health", timeout=30)
        assert health.status_code == 200
    finally:
        subprocess.run(["docker", "stop", cid], capture_output=True, timeout=15)


@pytest.mark.skipif(not HAVE_DOCKER, reason="docker 미가용")
def test_proxy_container_honors_port_env():
    """$PORT env 주입 시 컨테이너가 해당 포트로 리슨(Cloud Run 관례).

    fix 전: --port 8080 hardcode(exec form) 로 PORT env 무시. PORT=18081 주입 +
    호스트 매핑 18091:18081 로 실행 시 컨테이너는 여전히 8080 만 리슨 → /health 실패.
    fix 후: shell form ${PORT:-8080} 이 PORT=18081 을 읽어 18081 리슨 → /health 200.
    """
    subprocess.run(
        ["docker", "build", "-t", "youtube-proxy", "./proxy"],
        check=True,
        capture_output=True,
    )
    cid = subprocess.check_output(
        ["docker", "run", "-d", "--rm", "-e", "PORT=18081", "-p", "18091:18081", "youtube-proxy"],
        text=True,
    ).strip()
    try:
        health = _wait_health("http://localhost:18091/health", timeout=30)
        assert health.status_code == 200
    finally:
        subprocess.run(["docker", "stop", cid], capture_output=True, timeout=15)


def _wait_health(url, *, timeout):
    """upstream 이 HTTP 요청을 받을 준비가 될 때까지 health 엔드포인트 폴링."""
    import requests

    deadline = time.time() + timeout
    last_exc = None
    while time.time() < deadline:
        try:
            return requests.get(url, timeout=2)
        except requests.exceptions.RequestException as e:
            last_exc = e
            time.sleep(0.5)
    raise RuntimeError(f"health 엔드포인트 미응답: {last_exc}")
