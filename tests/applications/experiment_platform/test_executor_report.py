"""에이전트가 실험 결과를 서술한 `report.md`를 받는 경계를 검증한다.

전체 파이프라인에서 채점이 끝나 `metrics.json`이 나온 뒤, 그 숫자와 candidate diff를
Codex에 넘겨 리포트를 받는 구간이다. 실제 Codex 인증·추론은 실행하지 않고, 임시
executable로 작업 디렉터리에 실제로 무엇이 놓였는지와 산출물 확인 결과를 관찰한다.
"""

from __future__ import annotations

import json
import os
from pathlib import Path
import subprocess
import sys

import pytest

from applications.experiment_platform.executor.codex_worker import CodexWorkerError
from applications.experiment_platform.executor.prompt import REPORT_SECTIONS, build_report_prompt
from applications.experiment_platform.executor.report import (
    DIFF_FILENAME,
    REPORT_FILENAME,
    ReportError,
    ReportInput,
    capture_candidate_diff,
    missing_report_sections,
    write_experiment_report,
)


_ISSUE_BODY = "### 가설\n\n특징 스케일링이 순위 지표를 올린다.\n"


def _git(repository: Path, *arguments: str) -> str:
    """테스트 fixture의 실제 Git 상태를 만들거나 읽는다."""
    result = subprocess.run(
        ("git", "-C", str(repository), *arguments),
        check=True,
        capture_output=True,
        text=True,
    )
    return result.stdout.strip()


def _repository_with_candidate(tmp_path: Path) -> tuple[Path, str, str]:
    """base commit과 candidate commit을 가진 독립 임시 Git repository를 만든다."""
    repository = tmp_path / "workspace" / "repository"
    repository.mkdir(parents=True)
    _git(repository, "init")
    _git(repository, "config", "user.name", "Experiment report test")
    _git(repository, "config", "user.email", "experiment-report@example.invalid")
    source = repository / "src" / "model.py"
    source.parent.mkdir()
    source.write_text("NUM_LEAVES = 31\n", encoding="utf-8")
    _git(repository, "add", "src/model.py")
    _git(repository, "commit", "-m", "base")
    base_sha = _git(repository, "rev-parse", "HEAD")
    source.write_text("NUM_LEAVES = 31\nSCALE_FEATURES = True\n", encoding="utf-8")
    _git(repository, "add", "src/model.py")
    _git(repository, "commit", "-m", "candidate")
    return repository, base_sha, _git(repository, "rev-parse", "HEAD")


def _codex_home(tmp_path: Path) -> Path:
    """codex_worker가 요구하는 regular `auth.json` 하나만 가진 auth source를 만든다."""
    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    auth = codex_home / "auth.json"
    auth.write_text("test-codex-auth\n", encoding="utf-8")
    auth.chmod(0o400)
    return codex_home


def _metrics(tmp_path: Path) -> Path:
    """게시 직전의 `metrics.json`을 결과 디렉터리에 만든다."""
    result_directory = tmp_path / "workspace" / "result"
    result_directory.mkdir(parents=True)
    metrics_path = result_directory / "metrics.json"
    metrics_path.write_text(
        json.dumps({"contract_version": "experiment-metrics-v1"}), encoding="utf-8"
    )
    return metrics_path


def _write_codex_executable(path: Path, body: str) -> None:
    """테스트별 관찰 코드를 가진 실제 ``codex`` executable을 만든다."""
    path.write_text(
        f"#!{sys.executable}\nfrom __future__ import annotations\n{body}\n",
        encoding="utf-8",
    )
    path.chmod(0o700)


def _report_body(missing: str | None = None) -> str:
    """계약이 요구한 절을 모두(또는 하나만 빼고) 가진 리포트 본문을 만든다."""
    return "\n\n".join(
        f"{section}\n\n내용" for section in REPORT_SECTIONS if section != missing
    )


def _input(tmp_path: Path) -> tuple[ReportInput, Path]:
    """실제 subprocess 실행에 사용할 검증된 리포트 입력을 만든다."""
    repository, base_sha, candidate_sha = _repository_with_candidate(tmp_path)
    metrics_path = _metrics(tmp_path)
    return (
        ReportInput(
            repository=repository,
            metrics_path=metrics_path,
            issue_body=_ISSUE_BODY,
            base_dev_sha=base_sha,
            candidate_sha=candidate_sha,
            codex_home=_codex_home(tmp_path),
            timeout_seconds=5,
        ),
        metrics_path.parent,
    )


def test_report_prompt_names_the_inputs_and_fixes_the_section_contract() -> None:
    """리포트의 숫자 출처와 절 구성은 프롬프트가 못 박아야 한다."""
    prompt = build_report_prompt(
        issue_body=_ISSUE_BODY,
        metrics_filename="metrics.json",
        diff_filename="candidate.diff",
        report_filename="report.md",
    )

    assert _ISSUE_BODY in prompt
    for section in REPORT_SECTIONS:
        assert section in prompt
    assert "metrics.json" in prompt
    assert "candidate.diff" in prompt
    # 숫자를 다시 계산하거나 지어내지 말라는 지시와, 판정이 파이프라인이 아니라 리포트의
    # 몫이라는 지시가 이 계약의 핵심이다.
    assert "the only source of numbers" in prompt
    assert "not a verdict" in prompt
    assert "split_matches" in prompt


def test_capture_candidate_diff_returns_the_change_between_the_two_conditions(
    tmp_path: Path,
) -> None:
    """리포트가 서술할 "무엇을 바꿨는가"의 출처는 기억이 아니라 실제 커밋이다."""
    repository, base_sha, candidate_sha = _repository_with_candidate(tmp_path)

    diff = capture_candidate_diff(
        repository, base_dev_sha=base_sha, candidate_sha=candidate_sha
    )

    assert "src/model.py" in diff
    assert "+SCALE_FEATURES = True" in diff


def test_capture_candidate_diff_reports_a_failure_without_git_output(
    tmp_path: Path,
) -> None:
    """Git stderr에는 경로가 섞이므로 사유 코드만 남긴다."""
    repository, base_sha, _candidate = _repository_with_candidate(tmp_path)

    with pytest.raises(ReportError, match="git_failed"):
        capture_candidate_diff(
            repository, base_dev_sha=base_sha, candidate_sha="f" * 40
        )


def test_write_experiment_report_hands_codex_the_metrics_and_the_diff(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """리포트를 쓰는 작업 디렉터리에 채점 결과와 diff가 함께 놓여야 한다."""
    config, result_directory = _input(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    observed = tmp_path / "observed.json"
    _write_codex_executable(
        bin_dir / "codex",
        "\n".join(
            [
                "import json, os",
                "from pathlib import Path",
                "cwd = Path(os.getcwd())",
                f"Path({str(observed)!r}).write_text(json.dumps({{",
                "    'cwd': str(cwd),",
                "    'names': sorted(path.name for path in cwd.iterdir()),",
                "    'diff': (cwd / 'candidate.diff').read_text(encoding='utf-8'),",
                "}), encoding='utf-8')",
                f"(cwd / 'report.md').write_text({_report_body()!r}, encoding='utf-8')",
            ]
        ),
    )
    # git은 실제로 실행돼야 하므로 기존 PATH 앞에 fake codex만 끼운다.
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = write_experiment_report(config)

    seen = json.loads(observed.read_text(encoding="utf-8"))
    assert Path(seen["cwd"]).resolve() == result_directory.resolve()
    assert seen["names"] == [DIFF_FILENAME, "metrics.json"]
    assert "+SCALE_FEATURES = True" in seen["diff"]
    assert result.path == result_directory / REPORT_FILENAME
    assert result.missing_sections == ()


def test_report_without_a_required_section_is_still_kept(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """형식이 어긋난 리포트가 리포트 없음보다 낫다 — 버리지 않고 무엇이 빠졌는지 남긴다."""
    config, result_directory = _input(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    body = _report_body(missing="## 결론")
    _write_codex_executable(
        bin_dir / "codex",
        "\n".join(
            [
                "import os",
                "from pathlib import Path",
                f"(Path(os.getcwd()) / 'report.md').write_text({body!r}, encoding='utf-8')",
            ]
        ),
    )
    # git은 실제로 실행돼야 하므로 기존 PATH 앞에 fake codex만 끼운다.
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = write_experiment_report(config)

    assert result.path == result_directory / REPORT_FILENAME
    assert result.missing_sections == ("## 결론",)


@pytest.mark.parametrize("body", ("", "   \n"))
def test_missing_or_empty_report_is_reported_as_no_report(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, body: str
) -> None:
    """빈 파일을 리포트로 취급하면 게시된 산출물이 "썼다"고 거짓말한다."""
    config, _result_directory = _input(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_codex_executable(
        bin_dir / "codex",
        "\n".join(
            [
                "import os",
                "from pathlib import Path",
                f"(Path(os.getcwd()) / 'report.md').write_text({body!r}, encoding='utf-8')",
            ]
        ),
    )
    # git은 실제로 실행돼야 하므로 기존 PATH 앞에 fake codex만 끼운다.
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = write_experiment_report(config)

    assert result.path is None
    assert result.missing_sections == REPORT_SECTIONS


def test_a_stale_report_from_a_retried_job_is_not_adopted(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """workspace volume은 재시도에도 남는다 — 앞선 실행의 리포트를 이번 산출물로 쓰지 않는다."""
    config, result_directory = _input(tmp_path)
    (result_directory / REPORT_FILENAME).write_text(
        _report_body(), encoding="utf-8"
    )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_codex_executable(bin_dir / "codex", "pass")
    # git은 실제로 실행돼야 하므로 기존 PATH 앞에 fake codex만 끼운다.
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = write_experiment_report(config)

    assert result.path is None
    assert not (result_directory / REPORT_FILENAME).exists()


def test_report_requires_the_metrics_it_is_supposed_to_describe(tmp_path: Path) -> None:
    """채점 결과 없이 리포트를 쓰게 하면 숫자가 아니라 추측이 게시된다."""
    config, result_directory = _input(tmp_path)
    (result_directory / "metrics.json").unlink()

    with pytest.raises(ReportError, match="metrics_missing"):
        write_experiment_report(config)


def test_codex_failure_propagates_with_its_output_for_diagnosis(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """결과가 없는 실패 경로가 오히려 원문이 가장 필요한 곳이다(#612)."""
    config, _result_directory = _input(tmp_path)
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_codex_executable(
        bin_dir / "codex",
        "\n".join(["import sys, time", "print('report-marker', file=sys.stderr)", "sys.stderr.flush()", "time.sleep(30)"]),
    )
    # git은 실제로 실행돼야 하므로 기존 PATH 앞에 fake codex만 끼운다.
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    with pytest.raises(CodexWorkerError, match="codex_timeout") as raised:
        write_experiment_report(config)

    assert "report-marker" in raised.value.stderr


def test_missing_report_sections_lists_only_what_the_contract_asked_for() -> None:
    """검사 대상과 프롬프트가 요구한 절이 같은 목록에서 나와야 한다."""
    assert missing_report_sections(_report_body()) == ()
    assert missing_report_sections("## 가설\n") == REPORT_SECTIONS[1:]


def test_a_symlink_named_report_is_not_published(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """`report.md`가 symlink면 게시가 링크 대상을 그대로 올린다.

    Codex는 `danger-full-access`로 돌고 이 container에는 push token과 API token이
    mount돼 있다. `read_text`도 `publish_results`의 `is_file()`도 `upload_from_filename`도
    모두 symlink를 따라가므로, 게시 전에 regular file인지부터 확인해야 한다.
    """
    config, result_directory = _input(tmp_path)
    secret = tmp_path / "push-token"
    secret.write_text("ghs-installation-token\n", encoding="utf-8")
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    _write_codex_executable(
        bin_dir / "codex",
        "\n".join(
            [
                "import os",
                "from pathlib import Path",
                f"(Path(os.getcwd()) / 'report.md').symlink_to({str(secret)!r})",
            ]
        ),
    )
    # git은 실제로 실행돼야 하므로 기존 PATH 앞에 fake codex만 끼운다.
    monkeypatch.setenv("PATH", f"{bin_dir}:{os.environ['PATH']}")

    result = write_experiment_report(config)

    assert result.path is None
    assert result.reason == "report_not_a_regular_file"
    # 링크는 남겨 두더라도 게시 대상이 아니어야 한다는 것이 계약이다.
    assert (result_directory / REPORT_FILENAME).is_symlink()
