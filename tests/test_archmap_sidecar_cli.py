import json
import subprocess
import sys
from pathlib import Path
from typing import TypedDict

import pytest
import yaml


ROOT = Path(__file__).resolve().parents[1]
CLI = [sys.executable, "-m", "tools.archmap"]
WORKFLOW_PATH = ROOT / ".github" / "workflows" / "archmap.yml"
_WorkflowStep = TypedDict(
    "_WorkflowStep",
    {"run": str, "continue-on-error": str},
    total=False,
)


def _load_archmap_workflow() -> list[_WorkflowStep]:
    workflow = yaml.load(
        WORKFLOW_PATH.read_text(encoding="utf-8"),
        Loader=yaml.BaseLoader,
    )
    return workflow["jobs"]["pr-report"]["steps"]


def _run_text(step: _WorkflowStep) -> str:
    return step.get("run") or ""


def _run_cli(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [*CLI, *args],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )


def _write_delta(path: Path, raw: str | bytes) -> Path:
    if isinstance(raw, bytes):
        path.write_bytes(raw)
    else:
        path.write_text(raw, encoding="utf-8")
    return path


def test_check_sidecar_clean_is_silent(tmp_path: Path) -> None:
    # Given: sidecar_stale가 빈 배열인 유효한 delta JSON이다.
    delta = _write_delta(tmp_path / "clean.json", '{"sidecar_stale": []}')

    # When: 실제 CLI subprocess로 check-sidecar를 실행한다.
    result = _run_cli("check-sidecar", "--delta", str(delta))

    # Then: 표준 출력·오류 없이 성공한다.
    assert result.returncode == 0
    assert result.stdout == ""
    assert result.stderr == ""


def test_check_sidecar_prints_sorted_stale_paths_once(tmp_path: Path) -> None:
    # Given: 중복 없이 순서가 섞인 stale 경로 목록이다.
    delta = _write_delta(
        tmp_path / "stale.json",
        json.dumps({"sidecar_stale": ["z/module.py", "a/module.py", "m/module.py"]}),
    )

    # When: 실제 CLI subprocess로 check-sidecar를 실행한다.
    result = _run_cli("check-sidecar", "--delta", str(delta))

    # Then: stdout는 비어 있고 stderr에는 POSIX 경로가 정렬되어 한 번씩 나온다.
    assert result.returncode == 1
    assert result.stdout == ""
    assert result.stderr.splitlines() == ["a/module.py", "m/module.py", "z/module.py"]


@pytest.mark.parametrize(
    "raw",
    [
        "{",
        "\ufeff{\"sidecar_stale\": []}",
    ],
    ids=["malformed-json", "utf8-bom-is-invalid-json-input"],
)
def test_check_sidecar_rejects_malformed_json_without_traceback(
    tmp_path: Path, raw: str,
) -> None:
    # Given: JSON 문법 또는 UTF-8 JSON 입력이 유효하지 않다.
    delta = _write_delta(tmp_path / "invalid.json", raw)

    # When: 실제 CLI subprocess로 입력을 검사한다.
    result = _run_cli("check-sidecar", "--delta", str(delta))

    # Then: 진단과 exit 2만 반환하고 traceback은 출력하지 않는다.
    assert result.returncode == 2
    assert result.stdout == ""
    assert result.stderr
    assert "Traceback" not in result.stderr


def test_check_sidecar_rejects_invalid_utf8_without_traceback(tmp_path: Path) -> None:
    # Given: UTF-8로 해석할 수 없는 delta 파일이다.
    delta = _write_delta(tmp_path / "invalid-utf8.json", b"\xff")

    # When: 실제 CLI subprocess로 입력을 검사한다.
    result = _run_cli("check-sidecar", "--delta", str(delta))

    # Then: UTF-8 경계 오류를 traceback 없이 exit 2로 진단한다.
    assert result.returncode == 2
    assert result.stdout == ""
    assert "Traceback" not in result.stderr
    assert "UTF-8" in result.stderr


@pytest.mark.parametrize("raw", ["null", "[]", '"text"', "1", "true"])
def test_check_sidecar_rejects_non_object_top_level_without_traceback(
    tmp_path: Path, raw: str,
) -> None:
    # Given: delta JSON의 최상위 값이 object가 아니다.
    delta = _write_delta(tmp_path / "top-level.json", raw)

    # When: 실제 CLI subprocess로 입력을 검사한다.
    result = _run_cli("check-sidecar", "--delta", str(delta))

    # Then: 전체 schema 검증 없이 top-level 경계 진단을 exit 2로 반환한다.
    assert result.returncode == 2
    assert result.stdout == ""
    assert "top-level" in result.stderr
    assert "Traceback" not in result.stderr


@pytest.mark.parametrize(
    "raw",
    [
        "{}",
        '{"sidecar_stale": null}',
        '{"sidecar_stale": "one"}',
        '{"sidecar_stale": [1]}',
        '{"sidecar_stale": [""]}',
        '{"sidecar_stale": ["same", "same"]}',
    ],
    ids=["missing", "null", "non-array", "non-string", "empty-string", "duplicate"],
)
def test_check_sidecar_rejects_invalid_sidecar_stale_without_traceback(
    tmp_path: Path, raw: str,
) -> None:
    # Given: sidecar_stale 필드가 계약의 경계 조건을 위반한다.
    delta = _write_delta(tmp_path / "invalid-field.json", raw)

    # When: 실제 CLI subprocess로 입력을 검사한다.
    result = _run_cli("check-sidecar", "--delta", str(delta))

    # Then: 필드 진단과 exit 2만 반환하고 traceback은 출력하지 않는다.
    assert result.returncode == 2
    assert result.stdout == ""
    assert "sidecar_stale" in result.stderr
    assert "Traceback" not in result.stderr


def test_check_sidecar_rejects_missing_path_without_traceback(tmp_path: Path) -> None:
    # Given: delta 경로가 존재하지 않는다.
    missing = tmp_path / "missing.json"

    # When: 실제 CLI subprocess로 존재하지 않는 경로를 검사한다.
    result = _run_cli("check-sidecar", "--delta", str(missing))

    # Then: 파일 오류를 traceback 없이 exit 2로 반환한다.
    assert result.returncode == 2
    assert result.stdout == ""
    assert str(missing) in result.stderr
    assert "Traceback" not in result.stderr


def test_check_sidecar_rejects_directory_path_without_traceback(tmp_path: Path) -> None:
    # Given: delta 경로가 일반 파일이 아니라 디렉터리다.
    directory = tmp_path / "delta-dir"
    directory.mkdir()

    # When: 실제 CLI subprocess로 디렉터리 경로를 검사한다.
    result = _run_cli("check-sidecar", "--delta", str(directory))

    # Then: 파일 I/O 오류를 traceback 없이 exit 2로 반환한다.
    assert result.returncode == 2
    assert result.stdout == ""
    assert str(directory) in result.stderr
    assert "Traceback" not in result.stderr


def test_check_sidecar_unreadable_path_is_diagnosed_without_traceback(
    tmp_path: Path,
) -> None:
    # Given: child process에서 Path.read_text가 PermissionError를 발생시킨다.
    target = tmp_path / "unreadable.json"
    probe = (
        "from pathlib import Path\n"
        "from tools.archmap.__main__ import main\n"
        "def fail(*args, **kwargs):\n"
        "    raise PermissionError('permission denied')\n"
        "Path.read_text = fail\n"
        f"main(['check-sidecar', '--delta', {str(target)!r}])\n"
    )

    # When: monkeypatch를 포함한 실제 Python subprocess를 실행한다.
    result = subprocess.run(
        [sys.executable, "-c", probe],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
    )

    # Then: 권한 오류를 traceback 없이 exit 2로 반환한다.
    assert result.returncode == 2
    assert result.stdout == ""
    assert str(target) in result.stderr
    assert "Traceback" not in result.stderr


def test_delta_requires_name_status_path(tmp_path: Path) -> None:
    # Given: delta의 기존 필수 입력은 모두 준비했지만 name-status를 생략했다.
    base = _write_delta(
        tmp_path / "base.json",
        '{"repo": "Autoresearch", "revision": "base", "modules": [], "contracts": []}',
    )
    head = _write_delta(
        tmp_path / "head.json",
        '{"repo": "Autoresearch", "revision": "head", "modules": [], "contracts": []}',
    )
    numstat = _write_delta(tmp_path / "numstat.txt", "")

    # When: 실제 delta subprocess를 name-status 없이 실행한다.
    result = _run_cli(
        "delta",
        "--base",
        str(base),
        "--head",
        str(head),
        "--numstat",
        str(numstat),
        "--pr",
        "187",
    )

    # Then: argparse가 required --name-status를 exit 2로 보고한다.
    assert result.returncode == 2
    assert result.stdout == ""
    assert "--name-status" in result.stderr
    assert "Traceback" not in result.stderr


def test_delta_passes_name_status_deletions_to_staleness_gate(tmp_path: Path) -> None:
    # Given: public 모듈이 manifest head에서 사라지고 실제 D name-status가 있다.
    module_path = "autoresearch/action_logs/schema.py"
    module = {
        "id": "action_logs.schema",
        "stage": "action_logs",
        "path": module_path,
        "role": None,
        "owns": [],
        "not_owns": [],
        "public_symbols": [{"name": "run", "kind": "function", "sig": "()", "line": 1}],
        "version_consts": {},
        "schema_fields": {},
        "imports": [],
    }
    base = _write_delta(
        tmp_path / "base.json",
        json.dumps({"repo": "Autoresearch", "revision": "base", "modules": [module],
                    "contracts": []}),
    )
    head = _write_delta(
        tmp_path / "head.json",
        json.dumps({"repo": "Autoresearch", "revision": "head", "modules": [],
                    "contracts": []}),
    )
    numstat = _write_delta(tmp_path / "numstat.txt", f"0\t0\t{module_path}\n")
    name_status = _write_delta(tmp_path / "name-status.txt", f"D\t{module_path}\n")

    # When: 실제 delta subprocess에 name-status 경로를 전달한다.
    result = _run_cli(
        "delta",
        "--base",
        str(base),
        "--head",
        str(head),
        "--numstat",
        str(numstat),
        "--name-status",
        str(name_status),
        "--pr",
        "187",
    )

    # Then: parser가 전달한 실제 삭제 증거로 stale 목록이 비워진다.
    assert result.returncode == 0
    assert result.stderr == ""
    delta = json.loads(result.stdout)
    assert delta["sidecar_stale"] == []


def test_delta_reads_nul_git_outputs_with_unicode_manifest_paths(
    tmp_path: Path,
) -> None:
    # Given: 수정된 Korean 경로와 실제 삭제된 Korean 경로가 manifest에 있다.
    modified_path = "autoresearch/action_logs/수정.py"
    deleted_path = "autoresearch/action_logs/삭제.py"
    modified_base = {
        "id": "action_logs.modified",
        "stage": "action_logs",
        "path": modified_path,
        "role": None,
        "owns": [],
        "not_owns": [],
        "public_symbols": [
            {"name": "run", "kind": "function", "sig": "()", "line": 1},
        ],
        "version_consts": {},
        "schema_fields": {},
        "imports": [],
    }
    modified_head = {
        **modified_base,
        "public_symbols": [
            {"name": "run", "kind": "function", "sig": "(value)", "line": 1},
        ],
    }
    deleted_module = {
        **modified_base,
        "id": "action_logs.deleted",
        "path": deleted_path,
    }
    base = _write_delta(
        tmp_path / "base.json",
        json.dumps(
            {
                "repo": "Autoresearch",
                "revision": "base",
                "modules": [modified_base, deleted_module],
                "contracts": [],
            }
        ),
    )
    head = _write_delta(
        tmp_path / "head.json",
        json.dumps(
            {
                "repo": "Autoresearch",
                "revision": "head",
                "modules": [modified_head],
                "contracts": [],
            }
        ),
    )
    numstat = _write_delta(
        tmp_path / "numstat.z",
        f"1\t1\t{modified_path}\0".encode()
        + f"0\t1\t{deleted_path}\0".encode(),
    )
    name_status = _write_delta(
        tmp_path / "name-status.z",
        f"M\0{modified_path}\0D\0{deleted_path}\0".encode(),
    )

    # When: 실제 delta subprocess가 NUL-delimited Git 파일을 읽는다.
    result = _run_cli(
        "delta",
        "--base",
        str(base),
        "--head",
        str(head),
        "--numstat",
        str(numstat),
        "--name-status",
        str(name_status),
        "--pr",
        "187",
    )

    # Then: manifest의 literal POSIX 경로와 실제 삭제 판정이 그대로 유지된다.
    assert result.returncode == 0
    assert result.stderr == ""
    delta = json.loads(result.stdout)
    assert {module["path"] for module in delta["changed_modules"]} == {
        modified_path,
        deleted_path,
    }
    assert delta["sidecar_stale"] == [modified_path]


def test_archmap_workflow_collects_path_safe_git_metadata() -> None:
    # Given: PR 리포트 job의 워크플로우 단계가 YAML로 파싱된다.
    steps = _load_archmap_workflow()

    # When: name-status/numstat 수집 단계와 delta 실행 단계를 찾는다.
    name_status_steps = [
        step for step in steps if "git diff --name-status" in _run_text(step)
    ]
    numstat_steps = [
        step for step in steps if "git diff --numstat" in _run_text(step)
    ]
    delta_steps = [
        step
        for step in steps
        if "python -m tools.archmap delta" in _run_text(step)
    ]

    # Then: 두 Git 출력 모두 path-safe NUL 형식으로 수집해 delta에 전달한다.
    assert len(name_status_steps) == 1
    assert "git diff --name-status -z" in _run_text(name_status_steps[0])
    assert "/tmp/name-status.txt" in _run_text(name_status_steps[0])
    assert len(numstat_steps) == 1
    assert "git diff --numstat -z" in _run_text(numstat_steps[0])
    assert "/tmp/numstat.txt" in _run_text(numstat_steps[0])
    assert len(delta_steps) == 1
    assert "--name-status /tmp/name-status.txt" in _run_text(delta_steps[0])


def test_archmap_workflow_blocks_before_external_report_and_comment() -> None:
    # Given: PR 리포트 job의 단계 순서를 파싱한다.
    steps = _load_archmap_workflow()

    # When: name-status, delta, gate, server POST, PR 코멘트 단계를 식별한다.
    name_status_index = next(
        index
        for index, step in enumerate(steps)
        if "git diff --name-status" in _run_text(step)
    )
    delta_index = next(
        index
        for index, step in enumerate(steps)
        if "python -m tools.archmap delta" in _run_text(step)
    )
    gate_steps = [
        (index, step)
        for index, step in enumerate(steps)
        if "python -m tools.archmap check-sidecar" in _run_text(step)
    ]
    server_index = next(
        index
        for index, step in enumerate(steps)
        if "/api/pr-report" in _run_text(step)
    )
    comment_index = next(
        index
        for index, step in enumerate(steps)
        if "python -m tools.archmap comment" in _run_text(step)
    )

    # Then: gate가 같은 job에서 외부 부수효과보다 먼저 blocking으로 실행된다.
    assert len(gate_steps) == 1
    gate_index, gate_step = gate_steps[0]
    assert "--delta /tmp/pr-delta.json" in _run_text(gate_step)
    assert "continue-on-error" not in gate_step
    assert name_status_index < delta_index < gate_index < server_index < comment_index
