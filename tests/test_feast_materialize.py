"""feast_materialize 공개 batch 명령 테스트 (feast 불필요, dev 환경 실행 가능)."""

import json

import pytest

import autoresearch.jobs.feast_materialize as job
import feature_repo.bootstrap as bootstrap
from feature_repo.bootstrap import ensure_redis_ca_bundle


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


def test_missing_repo_path_exits_two(tmp_path, capsys):
    assert job.main(["--repo-path", str(tmp_path)]) == 2

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "failed"
    assert summary["error_type"] == "invalid_arguments"


def test_ensure_ca_bundle_uses_existing_path(tmp_path):
    ca = tmp_path / "ca.pem"
    ca.write_text("pem")
    env = {"REDIS_TLS_CA_PATH": str(ca)}

    assert ensure_redis_ca_bundle(env) == str(ca)


def test_ensure_ca_bundle_missing_path_without_secret_raises(tmp_path):
    env = {"REDIS_TLS_CA_PATH": str(tmp_path / "missing.pem")}

    with pytest.raises(RuntimeError):
        ensure_redis_ca_bundle(env)


def test_ensure_ca_bundle_without_config_returns_none():
    assert ensure_redis_ca_bundle({}) is None


def test_ensure_ca_bundle_fetches_secret(monkeypatch, tmp_path):
    monkeypatch.setattr(
        bootstrap, "_fetch_ca_secret", lambda project, secret: b"PEM"
    )
    env = {"REDIS_CA_SECRET_ID": "redis-ca", "GCP_PROJECT_ID": "proj"}

    path = ensure_redis_ca_bundle(env)

    assert path is not None
    assert env["REDIS_TLS_CA_PATH"] == path
    with open(path, "rb") as handle:
        assert handle.read() == b"PEM"


def test_ensure_ca_bundle_secret_without_project_raises():
    with pytest.raises(RuntimeError):
        ensure_redis_ca_bundle({"REDIS_CA_SECRET_ID": "redis-ca"})


def test_load_feature_store_constructs_sdk_store_without_connection(
    monkeypatch, tmp_path
):
    feast = pytest.importorskip("feast")
    captured: list[str] = []
    store = object()
    monkeypatch.setattr(
        feast,
        "FeatureStore",
        lambda *, repo_path: captured.append(repo_path) or store,
    )

    assert bootstrap.load_feature_store(tmp_path) is store
    assert captured == [str(tmp_path)]


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
    monkeypatch.setattr(job, "ensure_redis_ca_bundle", lambda env=None: None)
    monkeypatch.setattr(job, "load_feature_store", lambda repo_path: store)
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
