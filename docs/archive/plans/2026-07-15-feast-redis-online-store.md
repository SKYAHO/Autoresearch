# Feast Redis Cluster Online Store 연동 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

> Issue: #148 | 설계: `../specs/2026-07-15-feast-redis-online-store.md`

**Goal:** Memorystore Redis Cluster(IAM 인증·TLS·cluster 모드)를 Feast online store로 연동하고 materialize 공개 batch CLI·실행 이미지를 제공한다.

**Architecture:** Feast 0.64 `RedisOnlineStore`의 `_get_client`를 서브클래스에서 오버라이드해 redis-py `CredentialProvider`(google-auth IAM token)와 TLS kwargs를 주입한다. materialize는 배치 계약 v1 호환 추가 명령 `autoresearch.jobs.feast_materialize`로 제공하고, feast 그룹 전용 파생 이미지 `Dockerfile.feast`로 실행한다.

**Tech Stack:** feast 0.64.0, redis-py 5.x, google-auth, google-cloud-secret-manager, uv, pytest

**실행 환경 주의:** feast 그룹은 dev 그룹과 충돌한다. feast 관련 테스트는
`uv run --no-dev --group feast python -m pytest ...`로 실행하고, dev 환경의
기존 `uv run python -m pytest`에서는 `pytest.importorskip("feast")`로 skip된다.

---

### Task 1: feast 그룹 의존성 추가

**Files:**
- Modify: `pyproject.toml` (feast 그룹)
- Modify: `uv.lock` (재생성)

- [ ] **Step 1: pyproject.toml feast 그룹에 의존성 추가**

`pyproject.toml`의 `feast = [` 블록을 다음으로 교체한다.

```toml
feast = [
    "feast[gcp]==0.64.0",
    "google-cloud-bigquery>=3.20",
    "google-cloud-secret-manager>=2.20",
    "redis>=5.0",
    "pandas>=2.0",
    "pyarrow>=14.0",
    "python-dotenv>=1.0",
    "pytest>=8.0",
]
```

- [ ] **Step 2: lock 재생성 및 proxy export drift 확인**

Run:
```bash
uv lock
uv export --frozen --only-group proxy --no-hashes -o proxy/requirements.txt
git diff --exit-code proxy/requirements.txt
```
Expected: `uv lock` 성공, proxy/requirements.txt diff 없음 (exit 0)

- [ ] **Step 3: feast 그룹 환경 해석 확인**

Run: `uv sync --frozen --no-dev --group feast && uv run --no-sync python -c "import feast, redis, google.cloud.secretmanager, pytest; print('ok')"`
Expected: `ok`

- [ ] **Step 4: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "chore: feast 그룹에 secret-manager·pytest 의존성 추가 (#148)"
```

---

### Task 2: GCPIAMCredentialProvider (TDD)

**Files:**
- Create: `tests/test_redis_iam.py`
- Create: `feature_repo/redis_iam.py`

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_redis_iam.py`:

```python
"""feature_repo.redis_iam 어댑터 단위 테스트 (feast 그룹 환경 전용)."""

import sys
from datetime import datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

import pytest

pytest.importorskip("feast")

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from feature_repo import redis_iam  # noqa: E402


class _FakeCredentials:
    def __init__(self, *, token="token-1", valid=True, expiry=None):
        self.token = token
        self.valid = valid
        self.expiry = expiry
        self.refresh_calls = 0

    def refresh(self, request):
        self.refresh_calls += 1
        self.token = f"token-{self.refresh_calls + 1}"
        self.valid = True
        self.expiry = datetime.utcnow() + timedelta(hours=1)


def _provider(credentials):
    return redis_iam.GCPIAMCredentialProvider(
        credentials_factory=lambda: credentials
    )


def test_returns_cached_token_before_refresh_margin():
    credentials = _FakeCredentials(
        expiry=datetime.utcnow() + timedelta(hours=1)
    )
    provider = _provider(credentials)

    assert provider.get_credentials() == ("token-1",)
    assert credentials.refresh_calls == 0


def test_refreshes_token_near_expiry():
    credentials = _FakeCredentials(
        expiry=datetime.utcnow() + timedelta(minutes=1)
    )
    provider = _provider(credentials)

    assert provider.get_credentials() == ("token-2",)
    assert credentials.refresh_calls == 1


def test_refreshes_invalid_credentials():
    credentials = _FakeCredentials(valid=False)
    provider = _provider(credentials)

    assert provider.get_credentials() == ("token-2",)
    assert credentials.refresh_calls == 1


def test_empty_token_raises():
    credentials = _FakeCredentials(
        token="", expiry=datetime.utcnow() + timedelta(hours=1)
    )
    provider = _provider(credentials)

    with pytest.raises(RuntimeError):
        provider.get_credentials()
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run --no-dev --group feast python -m pytest tests/test_redis_iam.py -v`
Expected: FAIL — `ImportError: cannot import name 'redis_iam' from 'feature_repo'`

- [ ] **Step 3: 최소 구현 작성**

`feature_repo/redis_iam.py`:

```python
"""Memorystore Redis Cluster IAM 인증·TLS Feast online store 어댑터."""

from __future__ import annotations

import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Optional, Tuple

import google.auth
import google.auth.transport.requests
from redis.credentials import CredentialProvider

_TOKEN_REFRESH_MARGIN = timedelta(minutes=5)
_CLOUD_PLATFORM_SCOPE = "https://www.googleapis.com/auth/cloud-platform"


def _default_credentials() -> Any:
    credentials, _ = google.auth.default(scopes=[_CLOUD_PLATFORM_SCOPE])
    return credentials


class GCPIAMCredentialProvider(CredentialProvider):
    """연결 시점마다 유효한 IAM access token을 Redis AUTH 자격으로 제공한다."""

    def __init__(
        self, credentials_factory: Callable[[], Any] | None = None
    ) -> None:
        self._lock = threading.Lock()
        self._credentials = (credentials_factory or _default_credentials)()

    def get_credentials(self) -> Tuple[str]:
        with self._lock:
            if self._needs_refresh():
                request = google.auth.transport.requests.Request()
                self._credentials.refresh(request)
            token: Optional[str] = self._credentials.token
        if not token:
            raise RuntimeError("IAM access token could not be issued")
        return (token,)

    def _needs_refresh(self) -> bool:
        if not self._credentials.valid:
            return True
        expiry = self._credentials.expiry
        if expiry is None:
            return False
        # google-auth의 expiry는 naive UTC datetime이다.
        return expiry - datetime.utcnow() < _TOKEN_REFRESH_MARGIN
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run --no-dev --group feast python -m pytest tests/test_redis_iam.py -v`
Expected: 4 passed

- [ ] **Step 5: Commit**

```bash
git add tests/test_redis_iam.py feature_repo/redis_iam.py
git commit -m "feat: Redis IAM 인증 CredentialProvider 추가 (#148)"
```

---

### Task 3: IAMRedisOnlineStore 어댑터 (TDD)

**Files:**
- Modify: `tests/test_redis_iam.py` (테스트 추가)
- Modify: `feature_repo/redis_iam.py` (클래스 추가)

- [ ] **Step 1: 실패하는 테스트 추가**

`tests/test_redis_iam.py` 끝에 추가:

```python
def _config(**overrides):
    values = {
        "type": "feature_repo.redis_iam.IAMRedisOnlineStore",
        "redis_type": "redis_cluster",
        "connection_string": "10.10.16.3:6379",
    }
    values.update(overrides)
    return redis_iam.IAMRedisOnlineStoreConfig(**values)


def test_config_drops_unexpanded_env_placeholder():
    config = _config(tls_ca_cert_path="${REDIS_TLS_CA_PATH}")

    assert config.tls_ca_cert_path is None


def test_get_client_injects_iam_and_tls_kwargs(monkeypatch, tmp_path):
    ca_path = tmp_path / "ca.pem"
    ca_path.write_text("dummy")
    captured = {}

    def _fake_cluster(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(kind="cluster")

    monkeypatch.setattr(redis_iam, "RedisCluster", _fake_cluster)
    store = redis_iam.IAMRedisOnlineStore()
    fake_provider = SimpleNamespace(get_credentials=lambda: ("t",))
    store._credential_provider = fake_provider

    client = store._get_client(_config(tls_ca_cert_path=str(ca_path)))

    assert client.kind == "cluster"
    assert captured["credential_provider"] is fake_provider
    assert captured["ssl"] is True
    assert captured["ssl_ca_certs"] == str(ca_path)
    assert [(n.host, str(n.port)) for n in captured["startup_nodes"]] == [
        ("10.10.16.3", "6379")
    ]


def test_get_client_missing_ca_file_raises(monkeypatch, tmp_path):
    store = redis_iam.IAMRedisOnlineStore()
    store._credential_provider = SimpleNamespace()

    with pytest.raises(FileNotFoundError):
        store._get_client(
            _config(tls_ca_cert_path=str(tmp_path / "missing.pem"))
        )


def test_get_client_iam_auth_false_uses_parent(monkeypatch):
    import feast.infra.online_stores.redis as feast_redis

    captured = {}

    def _fake_redis(**kwargs):
        captured.update(kwargs)
        return SimpleNamespace(kind="plain")

    monkeypatch.setattr(feast_redis, "Redis", _fake_redis)
    store = redis_iam.IAMRedisOnlineStore()

    client = store._get_client(
        _config(redis_type="redis", iam_auth=False)
    )

    assert client.kind == "plain"
    assert "credential_provider" not in captured


def test_get_client_async_not_supported():
    import asyncio

    store = redis_iam.IAMRedisOnlineStore()

    with pytest.raises(NotImplementedError):
        asyncio.run(store._get_client_async(_config()))
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run --no-dev --group feast python -m pytest tests/test_redis_iam.py -v`
Expected: 신규 테스트 FAIL — `AttributeError: ... IAMRedisOnlineStoreConfig`

- [ ] **Step 3: 어댑터 구현 추가**

`feature_repo/redis_iam.py`의 import 블록을 다음으로 교체하고:

```python
from __future__ import annotations

import os
import threading
from datetime import datetime, timedelta
from typing import Any, Callable, Literal, Optional, Tuple

import google.auth
import google.auth.transport.requests
from feast.infra.online_stores.redis import (
    RedisOnlineStore,
    RedisOnlineStoreConfig,
    RedisType,
)
from pydantic import StrictStr, field_validator
from redis import Redis
from redis.cluster import ClusterNode, RedisCluster
from redis.credentials import CredentialProvider
```

파일 끝에 추가:

```python
class IAMRedisOnlineStoreConfig(RedisOnlineStoreConfig):
    """IAM 인증·TLS Redis Cluster용 online store 설정."""

    type: Literal["feature_repo.redis_iam.IAMRedisOnlineStore"] = (
        "feature_repo.redis_iam.IAMRedisOnlineStore"
    )

    iam_auth: bool = True
    """False면 부모 RedisOnlineStore 동작 그대로 사용한다 (로컬 테스트용)."""

    tls_ca_cert_path: Optional[StrictStr] = None
    """Redis 서버 CA 번들 파일 경로."""

    @field_validator("tls_ca_cert_path", mode="before")
    @classmethod
    def _drop_unexpanded_env(cls, value: object) -> object:
        # feature_store.yaml의 ${ENV}가 미설정으로 확장되지 않은 경우 None 처리.
        if isinstance(value, str) and value.startswith("${"):
            return None
        return value


class IAMRedisOnlineStore(RedisOnlineStore):
    """IAM token 인증과 TLS를 주입하는 RedisOnlineStore 확장."""

    _credential_provider: Optional[GCPIAMCredentialProvider] = None

    def _iam_kwargs(self, config: IAMRedisOnlineStoreConfig) -> dict[str, Any]:
        if self._credential_provider is None:
            self._credential_provider = GCPIAMCredentialProvider()
        kwargs: dict[str, Any] = {
            "credential_provider": self._credential_provider,
            "ssl": True,
        }
        if config.tls_ca_cert_path:
            if not os.path.exists(config.tls_ca_cert_path):
                raise FileNotFoundError(
                    f"Redis TLS CA bundle not found: {config.tls_ca_cert_path}"
                )
            kwargs["ssl_ca_certs"] = config.tls_ca_cert_path
        return kwargs

    def _get_client(self, online_store_config: IAMRedisOnlineStoreConfig):
        if not online_store_config.iam_auth:
            return super()._get_client(online_store_config)
        if not self._client:
            startup_nodes, kwargs = self._parse_connection_string(
                online_store_config.connection_string
            )
            kwargs.update(self._iam_kwargs(online_store_config))
            if online_store_config.redis_type == RedisType.redis_cluster:
                kwargs["startup_nodes"] = [
                    ClusterNode(**node) for node in startup_nodes
                ]
                self._client = RedisCluster(**kwargs)
            else:
                kwargs["host"] = startup_nodes[0]["host"]
                kwargs["port"] = startup_nodes[0]["port"]
                self._client = Redis(**kwargs)
        return self._client

    async def _get_client_async(
        self, online_store_config: IAMRedisOnlineStoreConfig
    ):
        # async 경로는 feature server 전용으로 이 연동 범위 밖이다.
        # 미검증 인증 경로를 쓰지 않도록 명시적으로 차단한다.
        raise NotImplementedError(
            "IAMRedisOnlineStore does not support the async client yet"
        )
```

- [ ] **Step 4: 테스트 통과 확인**

Run: `uv run --no-dev --group feast python -m pytest tests/test_redis_iam.py -v`
Expected: 9 passed

- [ ] **Step 5: dev 환경 skip 확인**

Run: `uv sync && uv run --no-sync python -m pytest tests/test_redis_iam.py -v`
Expected: skipped (feast 미설치)

- [ ] **Step 6: Commit**

```bash
git add tests/test_redis_iam.py feature_repo/redis_iam.py
git commit -m "feat: IAM 인증·TLS Redis Cluster online store 어댑터 추가 (#148)"
```

---

### Task 4: feature_store.yaml·.env.example 갱신

**Files:**
- Modify: `feature_repo/feature_store.yaml` (online_store 블록)
- Modify: `.env.example` (Redis 섹션)

- [ ] **Step 1: feature_store.yaml online_store 블록 교체**

기존:

```yaml
online_store:
  type: redis
  connection_string: ${REDIS_HOST}:${REDIS_PORT}
```

교체:

```yaml
# Online Store: Memorystore for Redis Cluster (IAM 인증·TLS)
# REDIS_HOST/REDIS_PORT 는 cluster discovery endpoint 값이다.
# 어댑터 구현·인증 방식은 feature_repo/redis_iam.py 참조.
online_store:
  type: feature_repo.redis_iam.IAMRedisOnlineStore
  redis_type: redis_cluster
  connection_string: ${REDIS_HOST}:${REDIS_PORT}
  tls_ca_cert_path: ${REDIS_TLS_CA_PATH}
```

- [ ] **Step 2: .env.example Redis 섹션 교체**

기존:

```text
# Memorystore for Redis (Online Store)
# 인스턴스 생성 후 콘솔에서 IP 확인
REDIS_HOST=localhost
REDIS_PORT=6379
```

교체:

```text
# Memorystore for Redis Cluster (Online Store)
# REDIS_HOST/REDIS_PORT: cluster discovery endpoint (infra terraform output)
# REDIS_TLS_CA_PATH: 서버 CA 번들 파일 경로 (없으면 REDIS_CA_SECRET_ID로 fetch)
# REDIS_CA_SECRET_ID: CA 번들을 저장한 Secret Manager secret id
REDIS_HOST=localhost
REDIS_PORT=6379
REDIS_TLS_CA_PATH=
REDIS_CA_SECRET_ID=
```

- [ ] **Step 3: Commit**

```bash
git add feature_repo/feature_store.yaml .env.example
git commit -m "feat: feature_store.yaml을 Redis Cluster IAM 어댑터 구성으로 전환 (#148)"
```

---

### Task 5: materialize CLI — CA 조달·파서 (TDD)

**Files:**
- Create: `tests/test_feast_materialize.py`
- Create: `autoresearch/jobs/feast_materialize.py`

feast import는 함수 내부로 미룬다. 파서·CA 조달·exit code 테스트는 feast 없이
dev 환경에서도 실행된다.

- [ ] **Step 1: 실패하는 테스트 작성**

`tests/test_feast_materialize.py`:

```python
"""feast_materialize 공개 batch 명령 테스트 (feast 불필요, dev 환경 실행 가능)."""

import json

import pytest

import autoresearch.jobs.feast_materialize as job


def _json_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line]


def test_version_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        job._build_parser().parse_args(["--version"])

    assert excinfo.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract_version"] == "batch-contract-v1"


@pytest.mark.parametrize(
    "argv",
    [
        ["--start-ts", "2026-07-01T00:00:00"],
        ["--end-ts", "2026-07-02T00:00:00"],
        ["--start-ts", "2026-07-02T00:00:00", "--end-ts", "2026-07-01T00:00:00"],
    ],
)
def test_invalid_ts_combination_exits_two(argv, capsys):
    assert job.main(argv) == 2

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "failed"
    assert summary["error_type"] == "invalid_arguments"


def test_invalid_views_exits_two(capsys):
    assert job.main(["--views", "a,,b"]) == 2


def test_ensure_ca_bundle_uses_existing_path(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("pem")
    env = {"REDIS_TLS_CA_PATH": str(ca)}

    assert job._ensure_ca_bundle(env) == str(ca)


def test_ensure_ca_bundle_missing_path_without_secret_raises(tmp_path):
    env = {"REDIS_TLS_CA_PATH": str(tmp_path / "missing.pem")}

    with pytest.raises(RuntimeError):
        job._ensure_ca_bundle(env)


def test_ensure_ca_bundle_without_config_returns_none():
    assert job._ensure_ca_bundle({}) is None


def test_ensure_ca_bundle_fetches_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(job, "_fetch_ca_secret", lambda project, secret: b"PEM")
    env = {"REDIS_CA_SECRET_ID": "redis-ca", "GCP_PROJECT_ID": "proj"}

    path = job._ensure_ca_bundle(env)

    assert path is not None
    assert env["REDIS_TLS_CA_PATH"] == path
    with open(path, "rb") as handle:
        assert handle.read() == b"PEM"


def test_ensure_ca_bundle_secret_without_project_raises():
    with pytest.raises(RuntimeError):
        job._ensure_ca_bundle({"REDIS_CA_SECRET_ID": "redis-ca"})
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run python -m pytest tests/test_feast_materialize.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'autoresearch.jobs.feast_materialize'`

- [ ] **Step 3: CLI 뼈대 구현 (파서 + CA 조달)**

`autoresearch/jobs/feast_materialize.py`:

```python
"""Feast offline store를 Redis online store로 materialize하는 공개 batch 명령."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import MutableMapping, Sequence

from autoresearch.jobs import BATCH_CONTRACT_VERSION

logger = logging.getLogger(__name__)
_REVISION = os.getenv("AUTORESEARCH_REVISION", "unknown")
JOB_NAME = "feast_materialize"


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


def _iso_datetime(value: str) -> datetime:
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError as exc:
        raise argparse.ArgumentTypeError("must be an ISO 8601 timestamp") from exc
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=UTC)
    return parsed


def _view_list(value: str) -> list[str]:
    views = [item.strip() for item in value.split(",")]
    if not views or any(not item for item in views):
        raise argparse.ArgumentTypeError(
            "must be a comma-separated feature view list"
        )
    return views


def _boolean(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value
    normalized = value.casefold()
    if normalized == "true":
        return True
    if normalized == "false":
        return False
    raise argparse.ArgumentTypeError("must be true or false")


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description=__doc__)
    parser.add_argument(
        "--version",
        action="version",
        version=json.dumps(
            {
                "application_revision": _REVISION,
                "contract_version": BATCH_CONTRACT_VERSION,
            },
            sort_keys=True,
        ),
    )
    parser.add_argument("--repo-path", default="feature_repo")
    parser.add_argument("--views", type=_view_list)
    parser.add_argument("--start-ts", type=_iso_datetime)
    parser.add_argument("--end-ts", type=_iso_datetime)
    parser.add_argument(
        "--dry-run",
        nargs="?",
        const=True,
        default=False,
        type=_boolean,
    )
    return parser


def _validate_args(args: argparse.Namespace) -> None:
    if (args.start_ts is None) != (args.end_ts is None):
        raise BatchArgumentError(
            "--start-ts and --end-ts must be provided together"
        )
    if args.start_ts is not None and args.start_ts >= args.end_ts:
        raise BatchArgumentError("--start-ts must be earlier than --end-ts")


def _fetch_ca_secret(project_id: str, secret_id: str) -> bytes:
    from google.cloud import secretmanager  # feast 그룹 의존성이라 지연 import

    client = secretmanager.SecretManagerServiceClient()
    name = f"projects/{project_id}/secrets/{secret_id}/versions/latest"
    response = client.access_secret_version(request={"name": name})
    return response.payload.data


def _ensure_ca_bundle(
    environment: MutableMapping[str, str] | None = None,
) -> str | None:
    env = os.environ if environment is None else environment
    ca_path = env.get("REDIS_TLS_CA_PATH", "").strip()
    if ca_path and Path(ca_path).exists():
        return ca_path
    secret_id = env.get("REDIS_CA_SECRET_ID", "").strip()
    if not secret_id:
        if ca_path:
            raise RuntimeError(f"Redis TLS CA bundle not found: {ca_path}")
        return None
    project_id = env.get("GCP_PROJECT_ID", "").strip()
    if not project_id:
        raise RuntimeError(
            "GCP_PROJECT_ID is required to fetch the Redis CA bundle"
        )
    payload = _fetch_ca_secret(project_id, secret_id)
    handle = tempfile.NamedTemporaryFile(
        mode="wb", suffix=".pem", delete=False
    )
    with handle:
        handle.write(payload)
    env["REDIS_TLS_CA_PATH"] = handle.name
    return handle.name
```

- [ ] **Step 4: 테스트 실패 재확인 (main 미구현)**

Run: `uv run python -m pytest tests/test_feast_materialize.py -v`
Expected: CA·파서 테스트 PASS, `job.main` 테스트만 FAIL (`AttributeError: main`)

- [ ] **Step 5: main·summary 구현**

`autoresearch/jobs/feast_materialize.py` 끝에 추가 (`_run`은 Task 6에서 구현하므로 여기서는 시그니처만 참조하는 형태가 아니라 아래 전체를 추가한다 — `_run`이 아직 없으므로 임시로 예외를 던지는 stub을 함께 추가한다):

```python
def _run(args: argparse.Namespace) -> dict[str, object]:
    raise NotImplementedError  # Task 6에서 구현


def _emit(payload: dict[str, object]) -> None:
    print(
        json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str),
        flush=True,
    )


def _summary(
    *, status: str, details: dict[str, object] | None = None
) -> dict[str, object]:
    payload: dict[str, object] = {
        "event": "job_summary",
        "contract_version": BATCH_CONTRACT_VERSION,
        "job": JOB_NAME,
        "status": status,
    }
    if details:
        payload.update(details)
    return payload


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 인자를 검증·실행하고 공개 종료 코드를 반환한다."""

    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
        _validate_args(args)
    except BatchArgumentError as exc:
        logger.error("Invalid feast_materialize arguments: %s", exc)
        _emit(
            _summary(
                status="failed", details={"error_type": "invalid_arguments"}
            )
        )
        return 2

    try:
        result = dict(_run(args))
    except Exception as exc:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("feast_materialize failed (%s)", type(exc).__name__)
        _emit(
            _summary(
                status="failed", details={"error_type": "runtime_failure"}
            )
        )
        return 1

    status = str(result.pop("status", "succeeded"))
    _emit(_summary(status=status, details=result))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 6: 테스트 통과 확인**

Run: `uv run python -m pytest tests/test_feast_materialize.py -v`
Expected: 전체 PASS

- [ ] **Step 7: Commit**

```bash
git add tests/test_feast_materialize.py autoresearch/jobs/feast_materialize.py
git commit -m "feat: feast_materialize 명령 파서·CA 조달 추가 (#148)"
```

---

### Task 6: materialize CLI — 실행 로직 (TDD)

**Files:**
- Modify: `tests/test_feast_materialize.py` (테스트 추가)
- Modify: `autoresearch/jobs/feast_materialize.py` (`_run` 구현)

- [ ] **Step 1: 실패하는 테스트 추가**

`tests/test_feast_materialize.py` 끝에 추가:

```python
class _FakeStore:
    def __init__(self, view_names):
        self._views = view_names
        self.calls = []
        self.config = type(
            "C",
            (),
            {
                "online_store": type(
                    "O",
                    (),
                    {"type": "feature_repo.redis_iam.IAMRedisOnlineStore"},
                )()
            },
        )()

    def list_feature_views(self):
        return [type("V", (), {"name": name})() for name in self._views]

    def materialize(self, start_date, end_date, feature_views):
        self.calls.append(("range", start_date, end_date, tuple(feature_views)))

    def materialize_incremental(self, end_date, feature_views):
        self.calls.append(("incremental", end_date, tuple(feature_views)))


@pytest.fixture
def fake_store(monkeypatch):
    store = _FakeStore(["UserStaticView", "VideoFeatureView"])
    monkeypatch.setattr(job, "_ensure_ca_bundle", lambda env=None: None)
    monkeypatch.setattr(job, "_load_store", lambda repo_path: store)
    return store


def test_incremental_materialize_all_views(fake_store, capsys):
    assert job.main([]) == 0

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "succeeded"
    assert summary["mode"] == "incremental"
    assert fake_store.calls[0][0] == "incremental"
    assert fake_store.calls[0][2] == ("UserStaticView", "VideoFeatureView")


def test_range_materialize_selected_views(fake_store, capsys):
    argv = [
        "--views",
        "UserStaticView",
        "--start-ts",
        "2026-07-01T00:00:00",
        "--end-ts",
        "2026-07-02T00:00:00",
    ]

    assert job.main(argv) == 0

    call = fake_store.calls[0]
    assert call[0] == "range"
    assert call[3] == ("UserStaticView",)


def test_unknown_view_exits_one(fake_store, capsys):
    assert job.main(["--views", "NopeView"]) == 1

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["error_type"] == "runtime_failure"


def test_dry_run_pings_online_store(fake_store, monkeypatch, capsys):
    pinged = []
    monkeypatch.setattr(
        job,
        "_online_client",
        lambda config: type(
            "R", (), {"ping": lambda self: pinged.append(True)}
        )(),
    )

    assert job.main(["--dry-run"]) == 0

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["mode"] == "dry_run"
    assert pinged == [True]
    assert fake_store.calls == []
```

- [ ] **Step 2: 테스트 실패 확인**

Run: `uv run python -m pytest tests/test_feast_materialize.py -v`
Expected: 신규 테스트 FAIL (`NotImplementedError` 또는 `AttributeError: _load_store`)

- [ ] **Step 3: `_run` 구현**

`autoresearch/jobs/feast_materialize.py`의 `_run` stub을 다음 전체로 교체:

```python
def _load_store(repo_path: str):
    resolved = Path(repo_path).resolve()
    if not (resolved / "feature_store.yaml").exists():
        raise BatchArgumentError(
            f"feature_store.yaml not found under {repo_path}"
        )
    parent = str(resolved.parent)
    if parent not in sys.path:
        # feature_store.yaml의 online_store type이
        # feature_repo.redis_iam 모듈 경로를 import할 수 있게 한다.
        sys.path.insert(0, parent)
    from feast import FeatureStore  # feast 그룹 의존성이라 지연 import

    return FeatureStore(repo_path=str(resolved))


def _online_client(online_config):
    import importlib

    type_path = str(online_config.type)
    if "." not in type_path:
        raise RuntimeError(
            "dry-run requires a custom online store adapter type"
        )
    module_path, class_name = type_path.rsplit(".", 1)
    store_class = getattr(importlib.import_module(module_path), class_name)
    return store_class()._get_client(online_config)


def _dry_run(store) -> dict[str, object]:
    views = sorted(view.name for view in store.list_feature_views())
    client = _online_client(store.config.online_store)
    client.ping()
    return {"mode": "dry_run", "views": views, "redis_ping": True}


def _run(args: argparse.Namespace) -> dict[str, object]:
    _ensure_ca_bundle()
    store = _load_store(args.repo_path)
    if args.dry_run:
        return {"status": "succeeded", **_dry_run(store)}

    registered = {view.name for view in store.list_feature_views()}
    views = args.views or sorted(registered)
    unknown = sorted(set(views) - registered)
    if unknown:
        raise RuntimeError(f"unknown feature views: {', '.join(unknown)}")

    end_ts = args.end_ts or datetime.now(UTC)
    if args.start_ts is not None:
        store.materialize(
            start_date=args.start_ts, end_date=end_ts, feature_views=views
        )
        mode = "range"
    else:
        store.materialize_incremental(end_date=end_ts, feature_views=views)
        mode = "incremental"
    return {
        "status": "succeeded",
        "mode": mode,
        "views": views,
        "start_ts": args.start_ts.isoformat() if args.start_ts else None,
        "end_ts": end_ts.isoformat(),
    }
```

(`main`의 `BatchArgumentError` 처리는 `parse_args`/`_validate_args`만 감싸므로,
`_run` 안에서 던진 `BatchArgumentError`도 `ValueError`라 exit 1 runtime 경로로
빠진다. repo-path 오류를 exit 2로 만들기 위해 `main`의 두 번째 try 블록을
다음으로 교체한다.)

```python
    try:
        result = dict(_run(args))
    except BatchArgumentError as exc:
        logger.error("Invalid feast_materialize arguments: %s", exc)
        _emit(
            _summary(
                status="failed", details={"error_type": "invalid_arguments"}
            )
        )
        return 2
    except Exception as exc:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("feast_materialize failed (%s)", type(exc).__name__)
        _emit(
            _summary(
                status="failed", details={"error_type": "runtime_failure"}
            )
        )
        return 1
```

- [ ] **Step 4: 테스트 통과 확인 (dev + feast 양쪽)**

Run:
```bash
uv run python -m pytest tests/test_feast_materialize.py -v
uv run --no-dev --group feast python -m pytest tests/test_feast_materialize.py tests/test_redis_iam.py -v
```
Expected: 양쪽 모두 전체 PASS

- [ ] **Step 5: 전체 회귀 확인**

Run: `uv run python -m pytest`
Expected: 기존 포함 전체 PASS (test_redis_iam은 skip)

- [ ] **Step 6: Commit**

```bash
git add tests/test_feast_materialize.py autoresearch/jobs/feast_materialize.py
git commit -m "feat: feast_materialize 실행 로직 구현 (#148)"
```

---

### Task 7: 배치 계약·spec 문서 갱신

**Files:**
- Modify: `docs/specs/2026-07-13-public-batch-execution-contract.md`
- Modify: `docs/specs/2026-07-15-feast-redis-online-store.md`

- [ ] **Step 1: 배치 계약 공개 명령 블록에 추가**

`## 공개 명령`의 코드 블록에 한 줄 추가:

```text
python -m autoresearch.jobs.feast_materialize [options]
```

목적 단락의 "Feature Store materialization, 학습·평가, MLflow, FastAPI serving
command는 각 기능이 운영화될 때 별도 revision으로 추가한다." 문장을 다음으로
교체:

```text
학습·평가, MLflow, FastAPI serving command는 각 기능이 운영화될 때 별도
revision으로 추가한다. Feature Store materialization은 아래 Feast
materialize 절에서 다룬다.
```

- [ ] **Step 2: 배치 계약에 Feast materialize 절 추가**

`## YouTube 일일 수집` 절 앞에 추가:

````markdown
## Feast materialize

```text
python -m autoresearch.jobs.feast_materialize \
  [--repo-path feature_repo] \
  [--views VIEW1,VIEW2] \
  [--start-ts ISO8601 --end-ts ISO8601] \
  [--dry-run[=<boolean>]]
```

### 계약

- v1 호환 추가 명령이다 (기존 명령의 계약 변경 없음).
- `--start-ts`/`--end-ts`는 함께만 지정할 수 있다. 하나만 지정하면 exit 2다.
- 둘 다 지정하면 해당 구간 materialize, 둘 다 없으면 현재 UTC 기준
  incremental materialize를 수행한다.
- `--views` 생략 시 registry의 전체 FeatureView가 대상이다. 등록되지 않은
  view 이름은 exit 1이다.
- `--dry-run`은 CA 조달, IAM token 발급, Redis `PING`, registry 접근까지만
  검증하고 적재 없이 exit 0으로 종료한다.
- IAM token, CA 본문, entity 값은 stdout·stderr에 출력하지 않는다.
- 실행 이미지는 `Dockerfile.feast` 파생 이미지다 (`Dockerfile.app`에는 feast
  의존성이 없다).

### 환경 변수

| 이름 | 용도 | 비고 |
| --- | --- | --- |
| `GCP_PROJECT_ID`, `BQ_DATASET`, `GCS_REGISTRY_PATH`, `GCS_STAGING_LOCATION` | Feast offline store·registry | 기존 Feast 설정 |
| `REDIS_HOST`, `REDIS_PORT` | Redis Cluster discovery endpoint | |
| `REDIS_TLS_CA_PATH` | 서버 CA 번들 파일 경로 | 선택 |
| `REDIS_CA_SECRET_ID` | CA 번들 Secret Manager secret id | `REDIS_TLS_CA_PATH` 부재 시 필수 |
````

- [ ] **Step 3: 연동 spec을 구현과 일치하게 보정**

`docs/specs/2026-07-15-feast-redis-online-store.md`에서:

1. `### 2. feature_store.yaml 갱신`의 yaml 블록에서 `iam_auth: ${REDIS_IAM_AUTH}` 줄 삭제 (미설정 env가 literal로 남아 bool 파싱이 깨지므로 yaml에서는 기본값 `true`를 사용하고, `iam_auth=false`는 테스트에서 config 직접 생성으로만 사용)
2. 환경 변수 표에서 `REDIS_IAM_AUTH` 행 삭제
3. `_get_client/_get_client_async` 언급 부분에 async는 미검증 인증 경로 차단을 위해 `NotImplementedError`로 명시적으로 막는다고 보정
4. 테스트 실행 명령을 `uv run --no-dev --group feast python -m pytest ...`로 정정

- [ ] **Step 4: 문서 검증**

Run: `git diff --check`
Expected: 출력 없음

- [ ] **Step 5: Commit**

```bash
git add docs/specs/2026-07-13-public-batch-execution-contract.md docs/specs/2026-07-15-feast-redis-online-store.md
git commit -m "docs: 배치 계약에 feast_materialize 명령 추가 및 spec 보정 (#148)"
```

---

### Task 8: 더미 데이터·조회 검증 스크립트 스키마 정합

**Files:**
- Modify: `scripts/generate_and_upload_dummy_data.py` (전면 교체)
- Modify: `scripts/verify_feature_retrieval.py` (전면 교체)

두 스크립트는 #97 이전의 구 스키마(`user_features`, `video_features`)를
사용하고 있어 현재 FeatureView와 불일치한다. GKE 검증의 사전 조건이므로 현재
`feature_repo/feature_definitions.py` 스키마로 교체한다.

- [ ] **Step 1: generate_and_upload_dummy_data.py 교체**

```python
"""
TEMP_FEAST_BOOTSTRAP:
실제 데이터 적재 파이프라인 완료 전 Feast 스키마/조회 검증용 임시 seed script.
실제 BigQuery 적재 파이프라인과 스키마가 확정되면 이 파일은 삭제한다.

더미 Feature 데이터 생성 및 BigQuery 직접 업로드.

업로드 대상 테이블 (feature_repo/feature_definitions.py와 정합):
  1. {project}.{dataset}.user_static_feature
  2. {project}.{dataset}.user_dynamic_feature
  3. {project}.{dataset}.video_feature
  4. {project}.{dataset}.user_category_similarity

사용법:
  uv run --no-dev --group feast python scripts/generate_and_upload_dummy_data.py
"""

import argparse
import os
import random
from datetime import UTC, datetime, timedelta

import pandas as pd
from dotenv import load_dotenv
from google.cloud import bigquery

CATEGORY_IDS = ["1", "10", "17", "20", "22", "23", "24", "25", "26", "28"]
AGE_GROUPS = ["10s", "20s", "30s", "40s", "50s+"]
OCCUPATIONS = ["student", "engineer", "designer", "manager", "etc"]
WATCH_TIME_BANDS = ["light", "medium", "heavy"]
TOPICS = ["music", "gaming", "sports", "news", "education", "entertainment"]


def _event_timestamp() -> datetime:
    return datetime.now(UTC) - timedelta(hours=1)


def generate_user_static(users: int) -> pd.DataFrame:
    ts = _event_timestamp()
    return pd.DataFrame(
        {
            "user_id": [f"user_{i:04d}" for i in range(users)],
            "event_timestamp": [ts] * users,
            "age_group": [random.choice(AGE_GROUPS) for _ in range(users)],
            "occupation": [random.choice(OCCUPATIONS) for _ in range(users)],
            "preferred_category": [
                random.sample(CATEGORY_IDS, k=3) for _ in range(users)
            ],
            "preferred_topics": [
                random.sample(TOPICS, k=2) for _ in range(users)
            ],
            "watch_time_band": [
                random.choice(WATCH_TIME_BANDS) for _ in range(users)
            ],
        }
    )


def generate_user_dynamic(users: int) -> pd.DataFrame:
    ts = _event_timestamp()
    clicks = [random.randint(0, 200) for _ in range(users)]
    views = [random.randint(0, 500) for _ in range(users)]
    likes = [random.randint(0, 50) for _ in range(users)]
    return pd.DataFrame(
        {
            "user_id": [f"user_{i:04d}" for i in range(users)],
            "event_timestamp": [ts] * users,
            "recent_click_count_7d": clicks,
            "recent_view_count_7d": views,
            "recent_watch_time_7d": [
                random.randint(0, 100_000) for _ in range(users)
            ],
            "recent_like_count_7d": likes,
            "historical_category_affinity": [
                random.choice(CATEGORY_IDS) for _ in range(users)
            ],
            "total_event_count_7d": [
                c + v + l for c, v, l in zip(clicks, views, likes)
            ],
        }
    )


def generate_video(videos: int) -> pd.DataFrame:
    ts = _event_timestamp()
    return pd.DataFrame(
        {
            "video_id": [f"video_{i:05d}" for i in range(videos)],
            "event_timestamp": [ts] * videos,
            "category_id": [
                random.choice(CATEGORY_IDS) for _ in range(videos)
            ],
            "duration_sec": [random.randint(30, 3600) for _ in range(videos)],
            "view_count": [
                random.randint(100, 10_000_000) for _ in range(videos)
            ],
            "like_ratio": [round(random.random(), 4) for _ in range(videos)],
            "comment_ratio": [
                round(random.random() / 10, 4) for _ in range(videos)
            ],
            "days_since_upload": [
                random.randint(0, 3650) for _ in range(videos)
            ],
            "channel_subscriber_count": [
                random.randint(0, 5_000_000) for _ in range(videos)
            ],
            "channel_view_count": [
                random.randint(0, 1_000_000_000) for _ in range(videos)
            ],
            "channel_video_count": [
                random.randint(1, 5000) for _ in range(videos)
            ],
        }
    )


def generate_user_category_similarity(users: int) -> pd.DataFrame:
    ts = _event_timestamp()
    rows = []
    for i in range(users):
        for category_id in random.sample(CATEGORY_IDS, k=3):
            rows.append(
                {
                    "user_id": f"user_{i:04d}",
                    "category_id": category_id,
                    "event_timestamp": ts,
                    "topic_similarity": round(random.random(), 4),
                    "topic_similarity_top_topic": random.choice(TOPICS),
                }
            )
    return pd.DataFrame(rows)


def upload(client: bigquery.Client, dataset: str, table: str, df: pd.DataFrame):
    table_id = f"{client.project}.{dataset}.{table}"
    job_config = bigquery.LoadJobConfig(write_disposition="WRITE_TRUNCATE")
    client.load_table_from_dataframe(df, table_id, job_config=job_config).result()
    print(f"[OK] {table_id}: {len(df)} rows")


def main() -> None:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--users", type=int, default=100)
    parser.add_argument("--videos", type=int, default=200)
    parser.add_argument(
        "--project", default=os.environ.get("GCP_PROJECT_ID")
    )
    parser.add_argument(
        "--dataset", default=os.environ.get("BQ_DATASET", "feast_offline_store")
    )
    args = parser.parse_args()
    if not args.project:
        raise SystemExit("GCP_PROJECT_ID (또는 --project)가 필요합니다")

    client = bigquery.Client(project=args.project)
    upload(client, args.dataset, "user_static_feature", generate_user_static(args.users))
    upload(client, args.dataset, "user_dynamic_feature", generate_user_dynamic(args.users))
    upload(client, args.dataset, "video_feature", generate_video(args.videos))
    upload(
        client,
        args.dataset,
        "user_category_similarity",
        generate_user_category_similarity(args.users),
    )


if __name__ == "__main__":
    main()
```

- [ ] **Step 2: verify_feature_retrieval.py 교체**

```python
"""
TEMP_FEAST_BOOTSTRAP:
현재 조회 검증은 임시 더미 데이터 기준이다.
실제 BigQuery 적재 파이프라인과 스키마가 확정되면 실제 데이터 기준으로 교체한다.

Feast Feature 조회 검증 스크립트.

사전 조건:
  1. feast apply 실행 완료
  2. python -m autoresearch.jobs.feast_materialize 실행 완료

사용법:
  uv run --no-dev --group feast python scripts/verify_feature_retrieval.py
"""

import pandas as pd
from dotenv import load_dotenv
from feast import FeatureStore


def verify_online_features(store: FeatureStore) -> None:
    print("=" * 60)
    print("1. Online Feature 조회 (get_online_features)")
    print("=" * 60)

    online_features = store.get_online_features(
        features=[
            "UserStaticView:age_group",
            "UserStaticView:watch_time_band",
            "UserDynamicView:recent_view_count_7d",
            "VideoFeatureView:view_count",
            "VideoFeatureView:like_ratio",
        ],
        entity_rows=[
            {"user_id": "user_0001", "video_id": "video_00001"},
            {"user_id": "user_0002", "video_id": "video_00002"},
        ],
    ).to_dict()

    df = pd.DataFrame(online_features)
    print(df.to_string(index=False))
    if df["age_group"].isna().all():
        raise SystemExit("[FAIL] online feature 값이 비어 있습니다")
    print(f"\n[OK] Online Feature 조회 성공 ({len(df)} rows)\n")


def verify_similarity_view(store: FeatureStore) -> None:
    print("=" * 60)
    print("2. 복합 Entity 조회 (UserCategorySimilarityView)")
    print("=" * 60)

    online_features = store.get_online_features(
        features=[
            "UserCategorySimilarityView:topic_similarity",
            "UserCategorySimilarityView:topic_similarity_top_topic",
        ],
        entity_rows=[{"user_id": "user_0001", "category_id": "10"}],
    ).to_dict()

    df = pd.DataFrame(online_features)
    print(df.to_string(index=False))
    print(f"\n[OK] 복합 Entity 조회 성공 ({len(df)} rows)\n")


def main() -> None:
    load_dotenv()
    store = FeatureStore(repo_path="feature_repo")
    verify_online_features(store)
    verify_similarity_view(store)


if __name__ == "__main__":
    main()
```

- [ ] **Step 3: 문법 확인**

Run: `uv run --no-dev --group feast python -m py_compile scripts/generate_and_upload_dummy_data.py scripts/verify_feature_retrieval.py`
Expected: 출력 없음 (exit 0)

- [ ] **Step 4: Commit**

```bash
git add scripts/generate_and_upload_dummy_data.py scripts/verify_feature_retrieval.py
git commit -m "fix: Feast seed·조회 검증 스크립트를 현행 FeatureView 스키마로 교체 (#148)"
```

---

### Task 9: Dockerfile.feast

**Files:**
- Create: `Dockerfile.feast`

- [ ] **Step 1: Dockerfile.feast 작성**

```dockerfile
FROM ghcr.io/astral-sh/uv:0.11.26 AS lock-export

WORKDIR /source

COPY pyproject.toml uv.lock ./
RUN ["/uv", "export", "--frozen", "--no-dev", "--group", "feast", "--no-hashes", "--output-file", "/requirements.lock"]

FROM python:3.12-slim

ARG VCS_REF=unknown

ENV PYTHONDONTWRITEBYTECODE=1
ENV PYTHONUNBUFFERED=1
ENV AUTORESEARCH_REVISION=${VCS_REF}

LABEL org.opencontainers.image.source="https://github.com/SKYAHO/Autoresearch" \
      org.opencontainers.image.revision="${VCS_REF}" \
      io.autoresearch.batch-contract.version="batch-contract-v1"

WORKDIR /app

RUN adduser --disabled-password --gecos "" appuser

COPY --from=lock-export /requirements.lock ./
RUN python -m pip install --no-cache-dir -r requirements.lock \
    && rm requirements.lock

COPY autoresearch ./autoresearch
COPY feature_repo ./feature_repo

USER appuser

CMD ["python", "-c", "import feast, feature_repo.redis_iam; print('autoresearch feast image ready')"]
```

- [ ] **Step 2: 로컬 빌드·스모크 검증**

Run:
```bash
docker build -f Dockerfile.feast -t autoresearch-feast:ci .
docker run --rm autoresearch-feast:ci
docker run --rm autoresearch-feast:ci python -m autoresearch.jobs.feast_materialize --help
docker run --rm autoresearch-feast:ci python -m autoresearch.jobs.feast_materialize --version
```
Expected: `autoresearch feast image ready`, help/version JSON 출력, 모두 exit 0

- [ ] **Step 3: Commit**

```bash
git add Dockerfile.feast
git commit -m "feat: feast 그룹 파생 실행 이미지 Dockerfile.feast 추가 (#148)"
```

---

### Task 10: CI feast job 추가

**Files:**
- Modify: `.github/workflows/ci.yml`

- [ ] **Step 1: pytest-feast job 추가**

`pytest` job 아래에 추가:

```yaml
  pytest-feast:
    name: pytest (feast group)
    runs-on: ubuntu-latest
    steps:
      - name: Checkout repository
        uses: actions/checkout@v6

      - name: Set up uv
        uses: astral-sh/setup-uv@v6
        with:
          version: "0.11.26"
          python-version: "3.12"
          enable-cache: true

      - name: Install dependencies
        # feast 그룹은 dev 그룹과 충돌하므로 --no-dev 로 격리 설치한다.
        run: uv sync --frozen --no-dev --group feast

      - name: Run feast pytest
        run: uv run --no-sync python -m pytest tests/test_redis_iam.py tests/test_feast_materialize.py -v
```

- [ ] **Step 2: docker-build job에 feast 이미지 추가**

`docker-build` job의 steps 끝에 추가:

```yaml
      - name: Build Feast Docker image
        # feast 그룹 파생 이미지 (materialize 실행·GKE 검증용)
        run: |
          docker build \
            --build-arg VCS_REF="${{ github.sha }}" \
            -f Dockerfile.feast \
            --tag autoresearch-feast:ci \
            .

      - name: Run Feast Docker image smoke check
        run: |
          docker run --rm autoresearch-feast:ci
          docker run --rm autoresearch-feast:ci \
            python -m autoresearch.jobs.feast_materialize --help
          docker run --rm autoresearch-feast:ci \
            python -m autoresearch.jobs.feast_materialize --version
```

- [ ] **Step 3: 워크플로우 검증**

Run:
```bash
git diff --check
command -v actionlint >/dev/null && actionlint .github/workflows/ci.yml || echo "actionlint 미설치 — skip"
```
Expected: diff check 출력 없음, actionlint 통과(설치 시)

- [ ] **Step 4: Commit**

```bash
git add .github/workflows/ci.yml
git commit -m "ci: feast 그룹 pytest·이미지 빌드 job 추가 (#148)"
```

---

### Task 11: 전체 검증 및 PR 준비

- [ ] **Step 1: 양쪽 환경 전체 테스트**

Run:
```bash
uv sync --frozen && uv run --no-sync python -m pytest -v
uv sync --frozen --no-dev --group feast && uv run --no-sync python -m pytest tests/test_redis_iam.py tests/test_feast_materialize.py -v
```
Expected: 전체 PASS

- [ ] **Step 2: 이미지 빌드 재확인**

Run: `docker build -f Dockerfile.app -t autoresearch:ci . && docker build -f Dockerfile.feast -t autoresearch-feast:ci .`
Expected: 둘 다 성공

- [ ] **Step 3: push**

```bash
git push origin feat/148-feast-redis-online-store
```

---

### Task 12: GKE 실제 검증

로컬에서 Redis Cluster에 접근할 수 없으므로 GKE `autoresearch` namespace에서
검증한다. 이 Task는 kubectl·이미지 push 권한이 필요하며, 레지스트리 위치와
환경 변수 실제 값은 실행 시점에 사용자·인프라 output에서 확정한다.

- [ ] **Step 1: 환경 값 확보**

```bash
# 인프라 terraform output (또는 인프라 담당에게 요청)
# 필요 값: redis_discovery_address, redis_discovery_port,
#          redis_server_ca_secret_id, GCP project id
gcloud redis clusters describe autoresearch-dev-redis-cluster \
  --region asia-northeast3 --format="value(discoveryEndpoints[0].address)"
```

- [ ] **Step 2: 이미지 push (레지스트리는 사용자와 확정)**

```bash
docker build --build-arg VCS_REF="$(git rev-parse HEAD)" \
  -f Dockerfile.feast -t <REGISTRY>/autoresearch-feast:148-validation .
docker push <REGISTRY>/autoresearch-feast:148-validation
```

- [ ] **Step 3: BigQuery 더미 데이터 적재 (로컬에서 실행 가능)**

```bash
uv run --no-dev --group feast python scripts/generate_and_upload_dummy_data.py
```

- [ ] **Step 4: 검증 pod 기동**

`/tmp` 대신 스크래치 디렉토리에 `feast-validation-pod.yaml` 작성:

```yaml
apiVersion: v1
kind: Pod
metadata:
  name: feast-redis-validation
  namespace: autoresearch
spec:
  serviceAccountName: autoresearch-app
  restartPolicy: Never
  containers:
    - name: feast
      image: <REGISTRY>/autoresearch-feast:148-validation
      command: ["sleep", "7200"]
      env:
        - name: GCP_PROJECT_ID
          value: "<PROJECT_ID>"
        - name: BQ_DATASET
          value: "feast_offline_store"
        - name: BQ_LOCATION
          value: "asia-northeast3"
        - name: GCS_REGISTRY_PATH
          value: "<GCS_REGISTRY_PATH>"
        - name: GCS_STAGING_LOCATION
          value: "<GCS_STAGING_LOCATION>"
        - name: REDIS_HOST
          value: "<DISCOVERY_ADDRESS>"
        - name: REDIS_PORT
          value: "6379"
        - name: REDIS_CA_SECRET_ID
          value: "<CA_SECRET_ID>"
```

```bash
kubectl apply -f feast-validation-pod.yaml
kubectl wait --for=condition=Ready pod/feast-redis-validation -n autoresearch --timeout=180s
```

- [ ] **Step 5: 연결 스모크 (dry-run)**

```bash
kubectl exec -n autoresearch feast-redis-validation -- \
  python -m autoresearch.jobs.feast_materialize --dry-run
```
Expected: `job_summary` `status=succeeded`, `redis_ping: true`, exit 0
(CA 조달 → IAM token AUTH → TLS PING 전 구간 검증)

- [ ] **Step 6: cluster hash slot 학습 검증 (infra #129 학습 목표)**

```bash
kubectl exec -i -n autoresearch feast-redis-validation -- python - <<'PY'
import os, sys
sys.path.insert(0, "/app")
from autoresearch.jobs.feast_materialize import _ensure_ca_bundle, _load_store, _online_client

_ensure_ca_bundle()
store = _load_store("/app/feature_repo")
client = _online_client(store.config.online_store)

print("PING:", client.ping())
shards = client.execute_command("CLUSTER SHARDS")
print("shard count:", len(shards))

k1, k2, k3 = "feature:{user:100}:age", "feature:{user:100}:watch", "feature:{user:200}:age"
print("same-tag slots:", client.keyslot(k1) == client.keyslot(k2))
client.set(k1, "a"); client.set(k2, "b"); client.set(k3, "c")
print("same-tag MGET:", client.execute_command("MGET", k1, k2))
try:
    client.execute_command("MGET", k1, k3)
    print("CROSSSLOT: NOT reproduced")
except Exception as exc:
    print("CROSSSLOT reproduced:", type(exc).__name__)
client.delete(k1, k2, k3)
PY
```
Expected: PING True, shard count 2, same-tag slot 일치·MGET 성공, 다른 tag
`CROSSSLOT` 재현

- [ ] **Step 7: feast apply + materialize + 조회 검증**

```bash
kubectl exec -n autoresearch feast-redis-validation -- \
  bash -c "cd /app/feature_repo && feast apply"
kubectl exec -n autoresearch feast-redis-validation -- \
  bash -c "cd /app && python -m autoresearch.jobs.feast_materialize"
kubectl cp scripts/verify_feature_retrieval.py \
  autoresearch/feast-redis-validation:/tmp/verify_feature_retrieval.py
kubectl exec -n autoresearch feast-redis-validation -- \
  bash -c "cd /app && python /tmp/verify_feature_retrieval.py"
```
Expected: apply 성공, materialize `status=succeeded` exit 0, 조회 스크립트
`[OK]` 2건 (Redis에서 실값 반환)

- [ ] **Step 8: 정리 및 결과 기록**

```bash
kubectl delete pod feast-redis-validation -n autoresearch
```

검증 결과(성공 로그 요약)를 이슈 #148 코멘트로 기록한다.
