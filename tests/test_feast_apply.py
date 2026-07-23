"""feast_apply 공개 batch 명령 테스트 (feast 불필요, dev 환경 실행 가능)."""

import json
import sys
from pathlib import Path

import pytest

import autoresearch.jobs.feast_apply as job


def _json_lines(output: str) -> list[dict[str, object]]:
    return [json.loads(line) for line in output.splitlines() if line]


class _FakeRepoConfig:
    project = "autoresearch_feature_store"


class _Recorder:
    """feast 호출과 사전 준비 순서를 기록하는 테스트 seam."""

    def __init__(self):
        self.order: list[str] = []
        self.apply_calls: list[tuple[object, Path, bool]] = []
        self.plan_calls: list[tuple[object, Path, bool]] = []
        self.sys_path_at_call: list[str] = []
        self.repo_config = _FakeRepoConfig()

    def ensure_redis_ca_bundle(self, environment=None):
        self.order.append("ca_bundle")
        return None

    def ensure_repo_importable(self, repo_path):
        self.order.append("repo_importable")
        return Path(repo_path).resolve()

    def load_repo_config(self, repo_path):
        self.order.append("load_repo_config")
        return self.repo_config

    def apply_total(self, repo_config, repo_path, skip_source_validation):
        self.order.append("apply_total")
        self.sys_path_at_call = list(sys.path)
        self.apply_calls.append((repo_config, repo_path, skip_source_validation))

    def plan(self, repo_config, repo_path, skip_source_validation):
        self.order.append("plan")
        self.sys_path_at_call = list(sys.path)
        self.plan_calls.append((repo_config, repo_path, skip_source_validation))


@pytest.fixture
def repo_path(tmp_path):
    repo = tmp_path / "feature_repo"
    repo.mkdir()
    (repo / "feature_store.yaml").write_text("project: autoresearch_feature_store\n")
    return repo


@pytest.fixture
def recorder(monkeypatch):
    fake = _Recorder()
    # _ensure_definitions_importable은 실제 구현을 실행하므로 sys.path를 격리한다.
    monkeypatch.setattr(sys, "path", list(sys.path))
    monkeypatch.setattr(job, "ensure_redis_ca_bundle", fake.ensure_redis_ca_bundle)
    monkeypatch.setattr(job, "ensure_repo_importable", fake.ensure_repo_importable)
    monkeypatch.setattr(job, "_load_repo_config", fake.load_repo_config)
    monkeypatch.setattr(job, "_apply_total", fake.apply_total)
    monkeypatch.setattr(job, "_plan", fake.plan)
    return fake


def test_ensure_repo_importable_inserts_parent_on_sys_path(tmp_path, monkeypatch):
    import sys

    from feature_repo.bootstrap import ensure_repo_importable

    repo = tmp_path / "feature_repo"
    repo.mkdir()
    monkeypatch.setattr(sys, "path", list(sys.path))

    resolved = ensure_repo_importable(repo)

    assert resolved == repo.resolve()
    assert sys.path[0] == str(repo.resolve().parent)


def test_version_flag_exits_zero(capsys):
    with pytest.raises(SystemExit) as excinfo:
        job._build_parser().parse_args(["--version"])

    assert excinfo.value.code == 0
    payload = json.loads(capsys.readouterr().out)
    assert payload["contract_version"] == "batch-contract-v1"


@pytest.mark.parametrize(
    "argv",
    [
        ["--dry-run=maybe"],
        ["--skip-source-validation=yes"],
        ["--unknown-flag"],
    ],
)
def test_invalid_arguments_exit_two(argv, capsys):
    assert job.main(argv) == 2

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["job"] == "feast_apply"
    assert summary["status"] == "failed"
    assert summary["error_type"] == "invalid_arguments"


def test_missing_feature_store_yaml_exits_two(tmp_path, capsys):
    assert job.main(["--repo-path", str(tmp_path)]) == 2

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "failed"
    assert summary["error_type"] == "invalid_arguments"


def test_apply_invokes_apply_total(recorder, repo_path, capsys):
    assert job.main(["--repo-path", str(repo_path)]) == 0

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "succeeded"
    assert summary["mode"] == "apply"
    assert summary["repo_path"] == str(repo_path)
    assert summary["project"] == "autoresearch_feature_store"
    assert summary["skip_source_validation"] is False
    assert recorder.apply_calls == [
        (recorder.repo_config, repo_path, False)
    ]
    assert recorder.plan_calls == []


def test_ca_bundle_and_sys_path_precede_repo_config_load(recorder, repo_path):
    assert job.main(["--repo-path", str(repo_path)]) == 0

    assert recorder.order == [
        "ca_bundle",
        "repo_importable",
        "load_repo_config",
        "apply_total",
    ]


def test_repo_dir_is_importable_when_feast_runs(recorder, repo_path):
    """feast의 parse_repo가 chdir 뒤 정의 파일을 최상위 module로 import한다."""

    assert job.main(["--repo-path", str(repo_path)]) == 0

    assert str(repo_path) in recorder.sys_path_at_call


def test_repo_dir_is_importable_for_dry_run(recorder, repo_path):
    assert job.main(["--repo-path", str(repo_path), "--dry-run"]) == 0

    assert str(repo_path) in recorder.sys_path_at_call


def test_skip_source_validation_is_forwarded(recorder, repo_path, capsys):
    argv = ["--repo-path", str(repo_path), "--skip-source-validation"]

    assert job.main(argv) == 0

    assert recorder.apply_calls[0][2] is True
    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["skip_source_validation"] is True


def test_dry_run_calls_plan_only(recorder, repo_path, capsys):
    assert job.main(["--repo-path", str(repo_path), "--dry-run"]) == 0

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "succeeded"
    assert summary["mode"] == "plan"
    assert recorder.plan_calls == [(recorder.repo_config, repo_path, False)]
    assert recorder.apply_calls == []


def test_feast_exception_is_not_swallowed(recorder, repo_path, monkeypatch, capsys):
    def _raise(repo_config, path, skip_source_validation):
        raise RuntimeError("FeastProviderLoginError")

    monkeypatch.setattr(job, "_apply_total", _raise)

    assert job.main(["--repo-path", str(repo_path)]) == 1

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "failed"
    assert summary["error_type"] == "runtime_failure"


def test_repo_config_load_failure_exits_one(recorder, repo_path, monkeypatch, capsys):
    def _raise(path):
        raise RuntimeError("FeastConfigError")

    monkeypatch.setattr(job, "_load_repo_config", _raise)

    assert job.main(["--repo-path", str(repo_path)]) == 1

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["error_type"] == "runtime_failure"
    assert recorder.apply_calls == []


def test_system_exit_from_apply_total_still_emits_summary(
    recorder, repo_path, monkeypatch, capsys
):
    def _exit(repo_config, path, skip_source_validation):
        raise SystemExit(1)

    monkeypatch.setattr(job, "_apply_total", _exit)

    assert job.main(["--repo-path", str(repo_path)]) == 1

    summary = _json_lines(capsys.readouterr().out)[-1]
    assert summary["status"] == "failed"
    assert summary["error_type"] == "runtime_failure"


def test_feast_stdout_is_redirected_to_stderr(recorder, repo_path, monkeypatch, capsys):
    def _chatty(repo_config, path, skip_source_validation):
        print("Applying changes for project autoresearch_feature_store")

    monkeypatch.setattr(job, "_apply_total", _chatty)

    assert job.main(["--repo-path", str(repo_path)]) == 0

    captured = capsys.readouterr()
    assert "Applying changes" in captured.err
    lines = _json_lines(captured.out)
    assert len(lines) == 1
    assert lines[-1]["event"] == "job_summary"


def test_cwd_is_restored_after_feast_chdir(recorder, repo_path, monkeypatch, tmp_path):
    import os

    def _chdir(repo_config, path, skip_source_validation):
        os.chdir(path)

    monkeypatch.setattr(job, "_apply_total", _chdir)
    origin = Path.cwd()

    assert job.main(["--repo-path", str(repo_path)]) == 0

    assert Path.cwd() == origin
