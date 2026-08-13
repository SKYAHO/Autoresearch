"""단일 파드 안에서 baseline·candidate 학습을 순서대로 실행하는 경계.

[파이프라인] workspace-preparer가 exp 브랜치를 checkout한 뒤부터 candidate-finalizer가
commit·push를 마친 뒤까지 — 같은 Pod의 workspace를 공유하며 두 조건의 학습을 실행하는
구간을 담당한다. baseline은 Codex 실행 **전**(dev 코드·dev 의존성), candidate는 push
**후**(candidate 코드·candidate 의존성)에 돈다.

[기능] workspace의 CLI 진입점을 subprocess로 호출해 seed별 학습을 반복하고, 의존성이
바뀐 경우에만 `uv sync`를 수행하며, 두 조건의 실행 순서를 state marker로 강제한다.
subprocess가 실패하거나 timeout되면 어느 호출이었는지와 출력 tail을 컨테이너 로그로
남긴다(#636) — 그 로그는 수집기가 `experiment_logs`로 옮겨 워크벤치가 읽는다(#559).

[비책임] 학습 알고리즘과 데이터셋 조립(`autoresearch/model_training/`), Codex 실행
(`codex_worker.py`), commit·push와 candidate 보고(`finalizer.py`·`api_client.py`), 스냅샷
GCS 게시(`autoresearch/model_training/training_snapshot_store.py`)는 담당하지 않는다.

[중요] 학습 코드는 이미지가 아니라 **workspace의 clone**에서 온다. 이미지에 파이프라인
코드를 구우면 Codex가 수정한 candidate 코드가 아니라 빌드 시점의 낡은 코드로 학습하게
되어 candidate 실험 자체가 무의미해진다 (#574).

[중요] 산출물은 clone **밖**에 쓴다(`output_root`, #603). clone 안에 쓰면 verifier가
그것을 Codex의 변경으로 수집해, 아무 변경도 없는 실행이 통과한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
import hashlib
import logging
from pathlib import Path
import re
import subprocess
from typing import Final

from applications.experiment_platform.executor.command_output import log_command_streams


_LOGGER = logging.getLogger(__name__)


_DEPENDENCY_PATHS: Final = ("pyproject.toml", "uv.lock")

# 데모 스코프에서 지원하지 않는 변경을 감지하는 경로 목록이다.
#
# 학습 입력은 **baseline 코드 기준으로 미리 조립해 얼려 둔** 스냅샷이다. candidate가
# 피처 정의를 바꾸면 그 피처는 스냅샷 CSV에 **존재하지 않는데, 학습은 에러 없이
# 통과한다** — 없는 컬럼이 그냥 안 쓰일 뿐이다. 결과적으로 candidate가 baseline과
# 사실상 같은 데이터로 학습되고, **실패로 보이지 않으면서 아무것도 검증하지 않은**
# 상태가 된다. 이 저장소가 지키는 "조용한 실패 금지" 원칙에 정면으로 어긋나므로
# 감지해서 거부한다.
#
# 근본 해결은 candidate 코드로 조립을 다시 하는 것인데, feast group이 executor
# 이미지에 없고 `pyproject.toml`이 feast와 dev를 conflicts로 선언해 재빌드로도 넣을 수
# 없다. 별도 컨테이너가 필요해 컨테이너 계약 변경까지 번지므로 데모 이후 과제로 둔다.
#
# ⚠️ **경로 목록은 잠정값이다.** 무엇을 "피처를 바꾸는 변경"으로 볼지는 feature store
# 소유자 확인이 필요하다(`feature_repo`·`model_contract`는 소유자 확인 없이 손대지
# 않는다는 작업 원칙이 있다). 여기서는 읽기만 하고 수정하지 않는다.
#
# 확정 전까지는 **넓은 쪽**으로 둔다. 좁으면 조용한 실패가 새고 넓으면 무관한 변경까지
# 막는데, **전자가 훨씬 위험하다** — 넓어서 막히면 명확한 사유와 함께 즉시 드러나지만,
# 좁아서 새면 아무 신호 없이 잘못된 결론이 나온다.
#
# 그래서 정의 파일만이 아니라 **피처를 계산하는 로직**까지 포함한다. 정의는 그대로 두고
# 계산만 바꾸는 변경도 스냅샷 컬럼을 낡게 만들기 때문이다. `feature_repo`는 파일 단위가
# 아니라 디렉터리 전체를 본다.
# #754 재배치로 build_training_dataset 의 경로가 바뀌었다. 이 pathspec 은 저장소 트리가
# 아니라 봉인된 base_dev_sha 워크스페이스에 적용되므로 재배치 이전 실험은 여전히 옛
# 경로를 가진다. 매칭되지 않는 pathspec 은 git diff 가 조용히 무시하므로 둘 다 둔다 —
# 위 "넓은 쪽으로 둔다" 원칙과 같은 판단이다.
_FEATURE_DEFINITION_PATHS: Final = (
    "feature_repo",
    "src/pipeline/build_training_dataset.py",
    "autoresearch/model_training/build_training_dataset.py",
)
_BASELINE_MARKER: Final = "baseline_training_complete"


@dataclass(frozen=True)
class _WorkspaceLayout:
    """workspace 트리가 어느 모듈 이름을 갖는지 나타낸다.

    이 세 값은 executor 이미지가 아니라 **봉인된 `base_dev_sha` 워크스페이스**에서
    실행된다(`_run(..., cwd=workspace)`). 이미지는 릴리스된 digest이고 워크스페이스는 그보다
    앞선 SHA일 수 있으므로, 이미지 버전이 아니라 **그 트리의 모양**을 보고 골라야 한다.
    """

    cli_module: str
    policy_seeds_module: str
    snapshot_store_module: str


# #754 재배치 이후 트리.
_CURRENT_LAYOUT: Final = _WorkspaceLayout(
    cli_module="autoresearch.cli",
    policy_seeds_module="autoresearch.model_evaluation.experiment_evaluation",
    snapshot_store_module="autoresearch.model_training.training_snapshot_store",
)
# 재배치 이전에 봉인된 트리. 진행 중 실험이 모두 끝나면 이 상수와 `workspace_layout`의
# 분기를 함께 제거한다 (#754).
_LEGACY_LAYOUT: Final = _WorkspaceLayout(
    cli_module="src.cli",
    policy_seeds_module="src.pipeline.experiment_evaluation",
    snapshot_store_module="src.pipeline.training_snapshot_store",
)


def workspace_layout(workspace: Path) -> _WorkspaceLayout:
    """봉인된 트리가 가진 모듈 이름 셋을 고른다.

    `autoresearch/cli.py`의 존재로 가른다. 이 셋(`cli`·`model_evaluation`·`model_training`)은
    **같은 커밋에서 함께 생겼으므로** 하나를 봐도 셋을 대표한다. `autoresearch/` 디렉터리
    자체는 재배치 이전에도 있었으므로 판별 기준이 될 수 없다.

    verifier의 `_tree_has`와 달리 여기서는 단일 술어로 충분한 이유가 이것이다 — 반면
    `applications/`는 다른 커밋에서 생기므로 그쪽은 경로마다 따로 봐야 한다.
    """
    if (workspace / "autoresearch" / "cli.py").is_file():
        return _CURRENT_LAYOUT
    return _LEGACY_LAYOUT


def _seed_probe(layout: _WorkspaceLayout) -> str:
    """workspace의 `POLICY_SEEDS`를 stdout으로 내는 한 줄 코드."""
    return (
        f"from {layout.policy_seeds_module} import POLICY_SEEDS;"
        "print(','.join(str(seed) for seed in POLICY_SEEDS))"
    )


def _download_probe(layout: _WorkspaceLayout) -> str:
    """스냅샷 다운로드도 workspace 코드에게 맡긴다.

    파이프라인 코드가 이미지에 없어 import할 수 없고, `by-hash` 레이아웃과 sidecar 복원
    규칙을 복제하면 사본이 늘어난다. 인자는 `python -c <code> <uri> <dir>` 형태로 argv에
    실어 넘긴다(shell 해석 없음).
    """
    return (
        "import sys;"
        "from pathlib import Path;"
        f"from {layout.snapshot_store_module} import download_snapshot;"
        "print(download_snapshot(dataset_uri=sys.argv[1], destination_dir=Path(sys.argv[2])))"
    )


_DATASET_CSV_NAME: Final = "training_dataset.csv"
_SHA256_PATTERN: Final = re.compile(r"^[0-9a-f]{64}$")


class TrainingError(RuntimeError):
    """학습 단계 실패 사유다. 명령 출력과 자격 증명은 포함하지 않는다."""


class TrainingStage(StrEnum):
    """두 조건 중 어느 쪽 학습인지 나타낸다."""

    BASELINE = "baseline"
    CANDIDATE = "candidate"


@dataclass(frozen=True)
class TrainingInput:
    """한 조건의 학습 실행에 필요한 workspace 좌표와 입력 데이터셋."""

    stage: TrainingStage
    workspace: Path
    dataset_path: Path
    output_root: Path
    state_directory: Path
    timeout_seconds: int

    def __post_init__(self) -> None:
        """잘못된 경로·timeout으로 subprocess를 띄우지 않게 fail-closed로 막는다."""
        if not self.workspace.is_dir():
            raise TrainingError("workspace_missing")
        if not self.dataset_path.is_file():
            raise TrainingError("dataset_missing")
        if not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise TrainingError("timeout_invalid")
        # 산출물이 clone 안에 떨어지면 verifier가 그것을 Codex의 변경으로 수집한다
        # (`git ls-files --others --exclude-standard`). model_*.txt·features_*.json·
        # categories_*.json은 gitignore에 걸리지 않아 그대로 노출되고, 그러면 Codex가
        # 아무 변경도 만들지 않은 실행이 `no_changes` 대신 통과한다 — 탐지되지 않는
        # 거짓 성공이라 실패보다 나쁘다. 경로 규칙을 주석이 아니라 계약으로 고정한다.
        workspace = self.workspace.resolve()
        output_root = self.output_root.resolve()
        if output_root == workspace or workspace in output_root.parents:
            raise TrainingError("output_root_inside_workspace")


def _run(argv: list[str], *, cwd: Path, timeout_seconds: int, stage: str) -> str:
    """workspace를 cwd로 subprocess를 실행하고 stdout만 돌려준다.

    실패하면 출력 tail을 **사유와 분리된 로그 줄**로 남긴다(#636). 사유에 붙이지 않는
    이유는 `phase2._safe_failure_reason`이 `^[a-z][a-z0-9_]*$`만 통과시켜 본문을 붙이면
    사유가 통째로 `redacted`가 되기 때문이다.

    `stage`는 호출 지점 이름이다. 이 함수는 seed probe·데이터셋 다운로드·`git diff`·
    `uv sync`·학습 다섯 곳이 쓰는데 사유 코드는 전부 같고, 로그 수집기가 붙이는
    `log_type`도 컨테이너 이름이라(#559) 다섯 단계가 한 값에 뭉친다. 단계를 가르는
    정보는 이 인자뿐이므로 기본값을 두지 않는다.
    """
    try:
        completed = subprocess.run(  # noqa: S603 - argv는 이 모듈이 조립한 고정 목록이다
            argv,
            cwd=cwd,
            timeout=timeout_seconds,
            capture_output=True,
            text=True,
            check=False,
        )
    except subprocess.TimeoutExpired as error:
        # POSIX의 `subprocess.run`은 이미 수집한 출력을 이 예외에 실어 던진다. 결과가
        # 없는 경로가 오히려 원문이 가장 필요한 곳이라 버리지 않는다(#636).
        _LOGGER.error(
            "training command timed out stage=%s timeout_sec=%d", stage, timeout_seconds
        )
        log_command_streams(
            _LOGGER,
            event="training output",
            stage=stage,
            stdout=error.stdout,
            stderr=error.stderr,
        )
        raise TrainingError("training_timeout") from error
    except OSError as error:
        # 출력이 없는 실패라 tail로는 잡히지 않는다. 사유 코드 하나로는 "명령이 이미지에
        # 없다"와 "권한이 없다"를 구분할 수 없는데 대응이 서로 다르다(#636). `filename`은
        # 싣지 않는다 — 경로를 로그에 남기지 않는 이 모듈의 규율을 따른다.
        _LOGGER.error(
            "training command could not start stage=%s error_type=%s reason=%s",
            stage,
            type(error).__name__,
            error.strerror,
        )
        raise TrainingError("training_spawn_failed") from error
    if completed.returncode != 0:
        _LOGGER.error(
            "training command failed stage=%s exit_code=%d", stage, completed.returncode
        )
        log_command_streams(
            _LOGGER,
            event="training output",
            stage=stage,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        # 사유는 접미사 없는 고정 코드로 둔다. `phase2._safe_failure_reason`이
        # `^[a-z][a-z0-9_]*$`에 맞는 값만 로그에 남기고 나머지는 `redacted`로 지우므로
        # (#583), 인자·경로를 붙이면 오히려 사유가 통째로 사라진다. 본문은 위의 별도
        # 로그 줄이 담당한다.
        raise TrainingError("training_command_failed")
    return completed.stdout


def expected_dataset_sha256(dataset_uri: str) -> str:
    """`by-hash/<sha256>/` URI에 박힌 기대 해시를 꺼낸다.

    스냅샷 주소가 곧 CSV 내용의 SHA-256이므로(#530), URI 자체가 무결성 기준이다.
    """
    segments = [segment for segment in dataset_uri.rstrip("/").split("/") if segment]
    if len(segments) < 2 or segments[-2] != "by-hash":
        raise TrainingError("dataset_uri_invalid")
    digest = segments[-1]
    if not _SHA256_PATTERN.fullmatch(digest):
        raise TrainingError("dataset_uri_invalid")
    return digest


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def ensure_dataset(
    *,
    dataset_uri: str,
    destination_dir: Path,
    workspace: Path,
    timeout_seconds: int,
) -> Path:
    """게시된 스냅샷을 1회 내려받아 검증된 로컬 CSV 경로를 돌려준다.

    **검증이 이 함수의 존재 이유다.** 다운로드 자체는 workspace 코드(`src/`)가 하는데
    그 경로는 Codex의 허용 범위(`src/**`)라 candidate가 바꿀 수 있다. 학습은 조건별로
    다른 코드로 도는 것이 목적이지만(#574), **데이터 조달은 두 조건이 같아야 한다** —
    baseline과 candidate가 다른 데이터로 학습하면 ROC-AUC 차이가 코드 변경 때문인지
    데이터 차이 때문인지 구분할 수 없어 paired 대조가 무효가 된다.

    그래서 받은 바이트의 SHA-256을 URI에 박힌 값과 대조한다. 원인이 코드 수정이든
    네트워크 오류든 잘못된 URI든, **결과가 다르면 잡힌다** — 원인을 열거할 필요가 없다.
    이 검증만 executor 이미지에 봉인되므로 candidate가 우회할 수 없다.

    이미 같은 해시의 파일이 있으면 다시 받지 않는다. baseline·candidate 두 단계와 Job
    재시도가 같은 파일을 공유하게 하려는 것이다(85MB 재다운로드 회피).
    """
    expected = expected_dataset_sha256(dataset_uri)
    dataset_path = destination_dir / _DATASET_CSV_NAME
    if dataset_path.is_file() and _sha256_file(dataset_path) == expected:
        return dataset_path

    destination_dir.mkdir(parents=True, exist_ok=True)
    raw = _run(
        ["python", "-c", _download_probe(workspace_layout(workspace)), dataset_uri, str(destination_dir)],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stage="dataset_download",
    ).strip()
    if not raw:
        raise TrainingError("dataset_download_empty")

    downloaded = Path(raw)
    if not downloaded.is_file():
        raise TrainingError("dataset_download_missing")
    if _sha256_file(downloaded) != expected:
        # 경로·해시는 사유에 싣지 않는다 — `_safe_failure_reason`이 고정 코드만 남긴다.
        raise TrainingError("dataset_hash_mismatch")
    return downloaded


def resolve_policy_seeds(workspace: Path, *, timeout_seconds: int = 60) -> tuple[int, ...]:
    """workspace 코드가 선언한 `POLICY_SEEDS`를 그대로 읽는다.

    executor 이미지에 `src/`가 없으므로 import가 불가능하고, 상수를 복제하면 세 번째
    사본이 되어 드리프트 위험이 생긴다(이미 `issue_authoring.py`가 복제본이라 전용
    드리프트 테스트를 두고 있다). 대신 workspace의 코드에게 직접 물어본다.

    조건별로 다른 값이 나오는 것이 **정상**이다 — candidate가 seed 정책을 바꾸는
    실험이라면 candidate 학습은 바뀐 값으로 돌아야 한다.
    """
    raw = _run(
        ["python", "-c", _seed_probe(workspace_layout(workspace))],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stage="seed_probe",
    ).strip()
    if not raw:
        raise TrainingError("policy_seeds_empty")
    try:
        seeds = tuple(int(token) for token in raw.split(","))
    except ValueError as error:
        raise TrainingError("policy_seeds_malformed") from error
    if not seeds:
        raise TrainingError("policy_seeds_empty")
    return seeds


def _changed_paths(
    workspace: Path, *, base_ref: str, paths: tuple[str, ...], timeout_seconds: int
) -> tuple[str, ...]:
    """`base_ref` 이후 주어진 경로 중 실제로 바뀐 것을 돌려준다."""
    changed = _run(
        ["git", "diff", "--name-only", base_ref, "--", *paths],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stage="git_diff",
    )
    return tuple(line.strip() for line in changed.splitlines() if line.strip())


def feature_definitions_changed(
    workspace: Path, *, base_ref: str, timeout_seconds: int = 60
) -> tuple[str, ...]:
    """candidate가 피처를 바꿨는지 확인하고 바뀐 경로를 돌려준다.

    **데모 스코프 제약을 코드로 강제하는 지점이다.** 학습 스냅샷이 baseline 코드 시점에
    파드 밖에서 미리 조립·고정되므로, 피처가 바뀌는 가설은 이 스냅샷으로 검증할 수 없다.
    그냥 진행하면 새 피처가 없는 데이터로 학습돼 candidate가 baseline과 같은 결과를 내고,
    **실패로 보이지 않으면서 아무것도 검증하지 않은** 상태가 된다.

    빈 tuple이면 지원 범위 안(피처 불변 가설)이다. 경로 목록은 feature store 소유자 확인
    전까지 잠정값이며 넓은 쪽으로 유지한다 — 좁으면 조용한 실패가 샌다.
    상세는 `_FEATURE_DEFINITION_PATHS` 주석 참조.
    """
    return _changed_paths(
        workspace,
        base_ref=base_ref,
        paths=_FEATURE_DEFINITION_PATHS,
        timeout_seconds=timeout_seconds,
    )


def dependencies_changed(workspace: Path, *, base_ref: str, timeout_seconds: int = 60) -> bool:
    """`base_ref` 이후 의존성 선언이 바뀌었는지 확인한다.

    바뀌지 않았으면 `uv sync`를 건너뛴다. 데모 규모에서 대부분의 candidate는
    하이퍼파라미터만 바꾸므로, 매번 동기화하면 deadline 예산만 축낸다.
    """
    return bool(
        _changed_paths(
            workspace,
            base_ref=base_ref,
            paths=_DEPENDENCY_PATHS,
            timeout_seconds=timeout_seconds,
        )
    )


def sync_dependencies(workspace: Path, *, timeout_seconds: int) -> None:
    """candidate가 바꾼 의존성을 workspace 환경에 반영한다.

    `--frozen`을 쓰지 않는다 — candidate가 `pyproject.toml`만 고치고 lock을 갱신하지
    않았을 수 있고, 그 경우 lock을 다시 풀어주는 것이 실험 의도에 맞다.
    """
    _run(
        ["uv", "sync"],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
        stage="uv_sync",
    )


def _marker_path(state_directory: Path) -> Path:
    return state_directory / _BASELINE_MARKER


def run_training(config: TrainingInput) -> tuple[int, ...]:
    """한 조건의 학습을 seed별로 실행하고 사용한 seed 목록을 돌려준다.

    candidate 학습은 baseline marker가 없으면 거부한다. 순서가 뒤집히면 baseline이
    candidate의 의존성으로 학습돼 두 조건의 차이가 "코드 변경"만이 아니게 되고 paired
    대조의 전제가 깨진다 — 재시도로 순서가 어긋나는 경로까지 막기 위해 marker를 쓴다.
    """
    marker = _marker_path(config.state_directory)
    if config.stage is TrainingStage.CANDIDATE and not marker.is_file():
        raise TrainingError("baseline_training_missing")

    seeds = resolve_policy_seeds(config.workspace)
    outputs = config.output_root / config.stage.value
    outputs.mkdir(parents=True, exist_ok=True)

    for seed in seeds:
        _run(
            [
                "python",
                "-m",
                workspace_layout(config.workspace).cli_module,
                "train-model",
                "--data-path",
                str(config.dataset_path),
                "--model-output",
                str(outputs / f"model_{seed}.txt"),
                "--test-set-output",
                str(outputs / f"test_{seed}.csv"),
                "--feature-columns-output",
                str(outputs / f"features_{seed}.json"),
                "--categorical-columns-output",
                str(outputs / f"categories_{seed}.json"),
                "--experiment",
                config.stage.value,
                "--split-seed",
                str(seed),
                "--model-seed",
                str(seed),
                "--sampler-seed",
                str(seed),
            ],
            cwd=config.workspace,
            timeout_seconds=config.timeout_seconds,
            stage=f"train_model:{config.stage.value}:{seed}",
        )

    if config.stage is TrainingStage.BASELINE:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(",".join(str(seed) for seed in seeds), encoding="utf-8")
    return seeds
