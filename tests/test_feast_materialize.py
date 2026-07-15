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
