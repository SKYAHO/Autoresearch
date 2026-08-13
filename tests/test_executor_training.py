"""단일 파드 baseline·candidate 학습의 순서 계약을 고정한다.

실제 학습을 돌리지 않고 subprocess 경계만 대역으로 바꿔, "순서가 뒤집히면 거부한다"는
계약과 seed 해석·의존성 동기화 판단을 검증한다. 학습 알고리즘 자체는
`tests/test_pipeline_*`의 범위다.
"""

from __future__ import annotations

import logging
from pathlib import Path
import sys

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from agent_orchestration.executor import training as training_module  # noqa: E402
from agent_orchestration.executor.training import (  # noqa: E402
    TrainingError,
    TrainingInput,
    TrainingStage,
    dependencies_changed,
    ensure_dataset,
    expected_dataset_sha256,
    feature_definitions_changed,
    resolve_policy_seeds,
    run_training,
)


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    """clone된 저장소를 흉내 내는 빈 디렉터리."""
    repository = tmp_path / "repository"
    repository.mkdir()
    return repository


@pytest.fixture
def dataset(tmp_path: Path) -> Path:
    path = tmp_path / "training_dataset.csv"
    path.write_text("a,b\n1,2\n", encoding="utf-8")
    return path


@pytest.fixture
def state_directory(tmp_path: Path) -> Path:
    path = tmp_path / "state"
    path.mkdir()
    return path


@pytest.fixture
def output_root(tmp_path: Path) -> Path:
    """clone(`workspace`)의 형제. 실제 Pod 배치와 같게 clone 밖에 둔다(#603)."""
    return tmp_path / "training-output"


def _stub_runs(monkeypatch: pytest.MonkeyPatch, *, seeds: str = "42,43,44") -> list[list[str]]:
    """`_run`을 대역으로 바꾸고 호출된 argv를 순서대로 모은다."""
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int, stage: str) -> str:
        calls.append(argv)
        return seeds if argv[:2] == ["python", "-c"] else ""

    monkeypatch.setattr(training_module, "_run", _fake_run)
    return calls


def _input(
    stage: TrainingStage,
    workspace: Path,
    dataset: Path,
    state: Path,
    outputs: Path | None = None,
) -> TrainingInput:
    return TrainingInput(
        stage=stage,
        workspace=workspace,
        dataset_path=dataset,
        output_root=outputs if outputs is not None else workspace.parent / "training-output",
        state_directory=state,
        timeout_seconds=600,
    )


def test_candidate_training_is_refused_before_baseline(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, dataset: Path, state_directory: Path
) -> None:
    """순서가 뒤집히면 학습을 시작하지 않는다.

    baseline이 candidate 의존성으로 돌면 두 조건의 차이가 코드 변경만이 아니게 되어
    paired 대조의 전제가 깨진다. 재시도로 순서가 어긋나는 경로까지 막아야 한다.
    """
    _stub_runs(monkeypatch)

    with pytest.raises(TrainingError, match="baseline_training_missing"):
        run_training(_input(TrainingStage.CANDIDATE, workspace, dataset, state_directory))


def test_baseline_then_candidate_succeeds_and_leaves_a_marker(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, dataset: Path, state_directory: Path
) -> None:
    _stub_runs(monkeypatch)

    baseline_seeds = run_training(
        _input(TrainingStage.BASELINE, workspace, dataset, state_directory)
    )
    assert baseline_seeds == (42, 43, 44)
    assert (state_directory / "baseline_training_complete").is_file()

    candidate_seeds = run_training(
        _input(TrainingStage.CANDIDATE, workspace, dataset, state_directory)
    )
    assert candidate_seeds == (42, 43, 44)


def test_training_runs_once_per_policy_seed(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, dataset: Path, state_directory: Path
) -> None:
    calls = _stub_runs(monkeypatch, seeds="7,8")

    run_training(_input(TrainingStage.BASELINE, workspace, dataset, state_directory))

    train_calls = [argv for argv in calls if "train-model" in argv]
    assert len(train_calls) == 2
    assert [argv[argv.index("--model-seed") + 1] for argv in train_calls] == ["7", "8"]


def test_seeds_come_from_the_workspace_not_a_local_constant(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    """seed 정책은 workspace 코드가 정한다.

    executor 이미지에 `src/`가 없어 import가 불가능하고, 상수를 복제하면 세 번째 사본이
    되어 드리프트가 생긴다. candidate가 seed 정책을 바꾸는 실험이라면 candidate 학습은
    바뀐 값으로 돌아야 하므로, workspace에게 직접 묻는 것이 의미상으로도 맞다.
    """
    _stub_runs(monkeypatch, seeds="1,2,3,4,5")

    assert resolve_policy_seeds(workspace) == (1, 2, 3, 4, 5)


@pytest.mark.parametrize("raw", ["", "   ", "not-a-seed"])
def test_malformed_seed_probe_output_is_rejected(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, raw: str
) -> None:
    """seed를 못 읽으면 학습을 시작하지 않는다 — 조용히 기본값으로 돌지 않는다."""
    _stub_runs(monkeypatch, seeds=raw)

    with pytest.raises(TrainingError, match="policy_seeds_"):
        resolve_policy_seeds(workspace)


def test_dependency_change_detection_uses_git_diff(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int, stage: str) -> str:
        calls.append(argv)
        return "uv.lock\n"

    monkeypatch.setattr(training_module, "_run", _fake_run)

    assert dependencies_changed(workspace, base_ref="abc123") is True
    assert calls[0][:4] == ["git", "diff", "--name-only", "abc123"]
    assert "pyproject.toml" in calls[0]
    assert "uv.lock" in calls[0]


def test_no_dependency_change_skips_sync(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    monkeypatch.setattr(
        training_module, "_run", lambda argv, *, cwd, timeout_seconds, stage: "\n"
    )

    assert dependencies_changed(workspace, base_ref="abc123") is False


def test_missing_dataset_is_rejected_before_spawning(
    workspace: Path, state_directory: Path, tmp_path: Path
) -> None:
    with pytest.raises(TrainingError, match="dataset_missing"):
        TrainingInput(
            stage=TrainingStage.BASELINE,
            workspace=workspace,
            dataset_path=tmp_path / "absent.csv",
            output_root=tmp_path / "training-output",
            state_directory=state_directory,
            timeout_seconds=600,
        )


def test_output_root_inside_the_clone_is_rejected(
    workspace: Path, dataset: Path, state_directory: Path
) -> None:
    """clone 안을 산출물 루트로 주면 거부한다(#603).

    verifier는 `git ls-files --others --exclude-standard`로 untracked를 수집하는데
    `model_*.txt`·`features_*.json`·`categories_*.json`은 gitignore에 걸리지 않는다.
    clone 안에 쓰면 Codex가 아무 변경도 만들지 않은 실행이 `no_changes` 대신 통과한다.
    """
    for inside in (workspace, workspace / "data" / "processed"):
        with pytest.raises(TrainingError, match="output_root_inside_workspace"):
            TrainingInput(
                stage=TrainingStage.BASELINE,
                workspace=workspace,
                dataset_path=dataset,
                output_root=inside,
                state_directory=state_directory,
                timeout_seconds=600,
            )


def test_outputs_are_written_outside_the_clone(
    monkeypatch: pytest.MonkeyPatch,
    workspace: Path,
    dataset: Path,
    state_directory: Path,
    output_root: Path,
) -> None:
    """산출물 경로가 clone 밖의 `output_root` 아래로만 조립된다(#603)."""
    calls = _stub_runs(monkeypatch, seeds="42")
    run_training(_input(TrainingStage.BASELINE, workspace, dataset, state_directory, output_root))

    written = [
        argv[index + 1]
        for argv in calls
        for index, token in enumerate(argv)
        if token
        in {
            "--model-output",
            "--test-set-output",
            "--feature-columns-output",
            "--categorical-columns-output",
        }
    ]
    assert written, "학습 산출물 경로가 argv에 실리지 않았다"
    for path in written:
        assert Path(path).is_relative_to(output_root / "baseline")
        assert not Path(path).is_relative_to(workspace)


_DIGEST = "d3d273e66324042cd8e547068c194231cf1812d53cb68236edba56b067055293"
_ROOT = "gs://bucket/training-snapshots"


def _uri_for(payload: bytes) -> str:
    import hashlib

    return f"{_ROOT}/by-hash/{hashlib.sha256(payload).hexdigest()}/"


@pytest.mark.parametrize(
    "uri",
    [
        f"{_ROOT}/{_DIGEST}/",  # by-hash 구간 없음
        f"{_ROOT}/by-hash/{_DIGEST[:-1]}/",  # 63자
        f"{_ROOT}/by-hash/NOTAHASH/",
        "",
    ],
)
def test_malformed_dataset_uri_is_rejected(uri: str) -> None:
    """by-hash 주소가 아니면 다운로드를 시도하기 전에 거부한다(#605)."""
    with pytest.raises(TrainingError, match="dataset_uri_invalid"):
        expected_dataset_sha256(uri)


def test_expected_sha256_is_read_from_the_uri() -> None:
    assert expected_dataset_sha256(f"{_ROOT}/by-hash/{_DIGEST}/") == _DIGEST
    assert expected_dataset_sha256(f"{_ROOT}/by-hash/{_DIGEST}") == _DIGEST


def test_downloaded_dataset_with_a_mismatched_hash_is_rejected(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, tmp_path: Path
) -> None:
    """받은 바이트가 주소와 다르면 실패한다 — 원인을 열거하지 않고 결과로 잡는다(#605).

    candidate가 `src/`의 다운로드 코드를 바꿔 다른 데이터를 받아오면 paired 대조가
    무효가 되는데, 이 검증만 executor 이미지에 있어 우회할 수 없다.
    """
    destination = tmp_path / "training-dataset"

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int, stage: str) -> str:
        target = destination / "training_dataset.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(b"tampered")
        return str(target)

    monkeypatch.setattr(training_module, "_run", _fake_run)
    with pytest.raises(TrainingError, match="dataset_hash_mismatch"):
        ensure_dataset(
            dataset_uri=_uri_for(b"original"),
            destination_dir=destination,
            workspace=workspace,
            timeout_seconds=600,
        )


def test_matching_dataset_is_accepted_and_reused_without_redownloading(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, tmp_path: Path
) -> None:
    """해시가 맞으면 통과하고, 두 번째 호출은 다시 받지 않는다(#605).

    baseline·candidate 두 단계와 Job 재시도가 같은 파일을 공유하게 하려는 것이다.
    """
    destination = tmp_path / "training-dataset"
    payload = b"original"
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int, stage: str) -> str:
        calls.append(argv)
        target = destination / "training_dataset.csv"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(payload)
        return str(target)

    monkeypatch.setattr(training_module, "_run", _fake_run)
    uri = _uri_for(payload)
    first = ensure_dataset(
        dataset_uri=uri, destination_dir=destination, workspace=workspace, timeout_seconds=600
    )
    second = ensure_dataset(
        dataset_uri=uri, destination_dir=destination, workspace=workspace, timeout_seconds=600
    )

    assert first.read_bytes() == payload
    assert second == destination / "training_dataset.csv"
    assert len(calls) == 1, "이미 받아둔 파일이 있는데 다시 내려받았다"
    assert calls[0][:2] == ["python", "-c"]
    assert calls[0][3] == uri, "URI는 코드 문자열이 아니라 argv로 넘겨야 한다"


def test_feature_definition_change_is_detected(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    """피처 정의가 바뀌면 바뀐 경로를 돌려준다.

    스냅샷은 baseline 코드 기준으로 얼려져 있어 새 피처가 CSV에 없다. 그런데 학습은
    에러 없이 통과하고 candidate가 baseline과 같은 데이터로 학습된다 — 실패로 보이지
    않으면서 아무것도 검증하지 않은 상태가 된다. 그래서 감지가 필요하다.
    """
    calls: list[list[str]] = []

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int, stage: str) -> str:
        calls.append(argv)
        return "feature_repo/feature_definitions.py\n"

    monkeypatch.setattr(training_module, "_run", _fake_run)

    changed = feature_definitions_changed(workspace, base_ref="abc123")

    assert changed == ("feature_repo/feature_definitions.py",)
    assert calls[0][:4] == ["git", "diff", "--name-only", "abc123"]
    assert "feature_repo" in calls[0]
    assert "src/pipeline/build_training_dataset.py" in calls[0]


def test_unchanged_feature_definitions_return_empty(
    monkeypatch: pytest.MonkeyPatch, workspace: Path
) -> None:
    """피처 불변 가설은 빈 tuple로 통과시킨다."""
    monkeypatch.setattr(
        training_module, "_run", lambda argv, *, cwd, timeout_seconds, stage: "\n"
    )

    assert feature_definitions_changed(workspace, base_ref="abc123") == ()


def test_failed_command_logs_its_stderr(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """실패한 명령의 stderr 본문이 로그에 남는다(#636).

    실험 #633은 `training_command_failed` 한 줄만 남기고 죽었고, 진짜 원인인
    `RecursionError`는 파이프 안에서 사라져 로컬 재현 20분을 요구했다.
    """
    with caplog.at_level(logging.ERROR, logger="agent_orchestration.executor.training"):
        with pytest.raises(TrainingError, match="training_command_failed"):
            training_module._run(
                [
                    sys.executable,
                    "-c",
                    "import sys; sys.stderr.write('RecursionError: marker\\n'); sys.exit(1)",
                ],
                cwd=workspace,
                timeout_seconds=30,
                stage="train_model",
            )

    assert "RecursionError: marker" in caplog.text


def test_failed_command_log_names_the_call_site(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """어느 호출이 죽었는지가 로그에 남는다(#636).

    `_run`은 seed probe·데이터셋 다운로드·`git diff`·`uv sync`·학습 다섯 곳이 쓰는데
    사유 코드는 전부 `training_command_failed` 하나로 뭉친다. 게다가 로그 수집기가
    붙이는 `log_type`은 컨테이너 이름이라(#559) 다섯 단계가 한 값에 모인다 — 단계를
    가르는 정보는 로그 줄 안에 우리가 직접 넣은 것뿐이다.
    """
    with caplog.at_level(logging.ERROR, logger="agent_orchestration.executor.training"):
        with pytest.raises(TrainingError, match="training_command_failed"):
            resolve_policy_seeds(workspace)

    assert "stage=seed_probe" in caplog.text
    assert "No module named 'autoresearch'" in caplog.text


def test_training_log_label_names_the_condition_and_seed(
    monkeypatch: pytest.MonkeyPatch, workspace: Path, dataset: Path, state_directory: Path
) -> None:
    """학습도 조건 2 × seed 3으로 여섯 번 돈다(#636).

    `train_model` 하나로만 표시하면 여섯 실행이 한 라벨에 뭉쳐, 어느 조건의 어느 seed가
    죽었는지 알 수 없다. 실험 #633은 candidate 학습이 죽은 경우였다.
    """
    stages: list[str] = []

    def _fake_run(argv: list[str], *, cwd: Path, timeout_seconds: int, stage: str) -> str:
        stages.append(stage)
        return "42,43" if argv[:2] == ["python", "-c"] else ""

    monkeypatch.setattr(training_module, "_run", _fake_run)

    run_training(_input(TrainingStage.BASELINE, workspace, dataset, state_directory))

    assert [stage for stage in stages if stage.startswith("train_model")] == [
        "train_model:baseline:42",
        "train_model:baseline:43",
    ]


def test_timed_out_command_logs_its_partial_output(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """timeout으로 죽인 명령의 부분 출력도 로그에 남는다(#636).

    POSIX의 `subprocess.run`은 이미 수집한 출력을 `TimeoutExpired`에 실어 던지므로
    버릴 이유가 없다. 결과가 없는 경로가 오히려 원문이 가장 필요한 곳이다.

    `text=True`를 줘도 **이 예외의 출력만은 bytes로 온다.** 그대로 찍으면 `b'…'`가 되어
    읽을 수 없으므로 디코드해야 한다.
    """
    with caplog.at_level(logging.ERROR, logger="agent_orchestration.executor.training"):
        with pytest.raises(TrainingError, match="training_timeout"):
            training_module._run(
                [
                    sys.executable,
                    "-c",
                    "import sys, time; sys.stderr.write('PARTIAL-MARKER\\n'); "
                    "sys.stderr.flush(); time.sleep(30)",
                ],
                cwd=workspace,
                timeout_seconds=1,
                stage="train_model:baseline:42",
            )

    assert "PARTIAL-MARKER" in caplog.text
    assert "stage=train_model:baseline:42" in caplog.text
    assert "b'" not in caplog.text


def test_logged_output_keeps_only_the_tail(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """상한을 넘는 출력은 뒤만 남긴다(#636).

    앞이 아니라 **뒤**를 남기는 이유는 오류가 출력의 끝에 오기 때문이다 — 트레이스백은
    항상 마지막에 찍힌다. 상한이 없으면 학습 진행 로그가 실패 한 건에 수 MB씩 실린다.
    """
    script = (
        "import sys; "
        "sys.stderr.write('HEAD-MARKER\\n'); "
        "sys.stderr.write('x' * 200000 + '\\n'); "
        "sys.stderr.write('TAIL-MARKER\\n'); "
        "sys.exit(1)"
    )
    with caplog.at_level(logging.ERROR, logger="agent_orchestration.executor.training"):
        with pytest.raises(TrainingError, match="training_command_failed"):
            training_module._run(
                [sys.executable, "-c", script],
                cwd=workspace,
                timeout_seconds=30,
                stage="train_model:baseline:42",
            )

    assert "TAIL-MARKER" in caplog.text
    assert "HEAD-MARKER" not in caplog.text


def test_raised_failure_survives_the_phase2_redaction_filter(workspace: Path) -> None:
    """**실제로 던져진** 예외가 `phase2._safe_failure_reason`을 통과해야 한다(#636).

    출력 본문을 사유 인자에 붙이면 `args`가 둘이 되어 필터가 통째로 `redacted`로
    지운다 — 로그를 얻으려다 사유까지 잃는 회귀를 막는다. 본문은 별도 로그 줄이 담당한다.

    예외를 여기서 만들어 넘기면 안 된다. 그러면 `_safe_failure_reason`의 성질만 확인할
    뿐, `_run`이 무엇을 던지는지는 검증하지 못한다.
    """
    # phase2는 GitHub App·GCS 의존성을 끌어오므로 이 테스트 안에서만 import한다.
    from agent_orchestration.executor import phase2

    with pytest.raises(TrainingError) as raised:
        training_module._run(
            [sys.executable, "-c", "import sys; sys.stderr.write('detail\\n'); sys.exit(1)"],
            cwd=workspace,
            timeout_seconds=30,
            stage="train_model:baseline:42",
        )

    assert phase2._safe_failure_reason(raised.value) == "training_command_failed"


def test_failure_logs_both_streams_even_when_one_is_empty(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """실패 시 stdout·stderr를 각각 남기고, 비어 있어도 한 줄을 남긴다(#636).

    `autoresearch/model_training/train.py`는 진행 상황을 `print()`로 내므로 `[Step 1]`~`[Step 8]`이
    stdout에 있다. 실험 #633 진단의 절반이 "8단계는 다 통과했고 마지막 ONNX 패키징에서
    죽었다"였고, 그 정보의 출처가 여기다.

    비어 있어도 한 줄을 남기는 이유는 "출력이 없었다"와 "로깅이 깨졌다"를 구분하기
    위해서다(#612 `_log_codex_output`과 같은 판단).
    """
    script = "import sys; sys.stdout.write('[Step 8] PROGRESS-MARKER\\n'); sys.exit(1)"
    with caplog.at_level(logging.ERROR, logger="agent_orchestration.executor.training"):
        with pytest.raises(TrainingError, match="training_command_failed"):
            training_module._run(
                [sys.executable, "-c", script],
                cwd=workspace,
                timeout_seconds=30,
                stage="train_model:baseline:42",
            )

    assert "[Step 8] PROGRESS-MARKER" in caplog.text
    assert "stream=stdout" in caplog.text
    assert "stream=stderr bytes=0" in caplog.text


def test_spawn_failure_logs_why_the_process_could_not_start(
    workspace: Path, caplog: pytest.LogCaptureFixture
) -> None:
    """프로세스가 아예 못 뜨면 그 사유가 로그에 남는다(#636).

    출력이 없는 실패라 tail로는 잡히지 않는다. `training_spawn_failed` 하나로는 "명령이
    이미지에 없다"와 "권한이 없다"를 구분할 수 없는데, 둘은 대응이 완전히 다르다.
    """
    with caplog.at_level(logging.ERROR, logger="agent_orchestration.executor.training"):
        with pytest.raises(TrainingError, match="training_spawn_failed"):
            training_module._run(
                ["definitely-not-a-real-command-636"],
                cwd=workspace,
                timeout_seconds=30,
                stage="uv_sync",
            )

    assert "stage=uv_sync" in caplog.text
    assert "error_type=FileNotFoundError" in caplog.text
