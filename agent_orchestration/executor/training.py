"""단일 파드 안에서 baseline·candidate 학습을 순서대로 실행하는 경계.

[파이프라인] workspace-preparer가 exp 브랜치를 checkout한 뒤부터 candidate-finalizer가
commit·push를 마친 뒤까지 — 같은 Pod의 workspace를 공유하며 두 조건의 학습을 실행하는
구간을 담당한다. baseline은 Codex 실행 **전**(dev 코드·dev 의존성), candidate는 push
**후**(candidate 코드·candidate 의존성)에 돈다.

[기능] workspace의 `src.cli` 를 subprocess로 호출해 seed별 학습을 반복하고, 의존성이
바뀐 경우에만 `uv sync`를 수행하며, 두 조건의 실행 순서를 state marker로 강제한다.

[비책임] 학습 알고리즘과 데이터셋 조립(`src/pipeline/`), Codex 실행(`codex_worker.py`),
commit·push와 candidate 보고(`finalizer.py`·`api_client.py`), 스냅샷 GCS 게시
(`src/pipeline/training_snapshot_store.py`)는 담당하지 않는다.

[중요] 학습 코드는 이미지가 아니라 **workspace의 clone**에서 온다. 이미지에 `src/`를
구우면 Codex가 수정한 candidate 코드가 아니라 빌드 시점의 낡은 코드로 학습하게 되어
candidate 실험 자체가 무의미해진다 (#574).

[중요] 산출물은 clone **밖**에 쓴다(`output_root`, #603). clone 안에 쓰면 verifier가
그것을 Codex의 변경으로 수집해, 아무 변경도 없는 실행이 통과한다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from pathlib import Path
import subprocess
from typing import Final


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
_FEATURE_DEFINITION_PATHS: Final = (
    "feature_repo",
    "src/pipeline/build_training_dataset.py",
)
_SEED_PROBE: Final = (
    "from src.pipeline.experiment_evaluation import POLICY_SEEDS;"
    "print(','.join(str(seed) for seed in POLICY_SEEDS))"
)
_BASELINE_MARKER: Final = "baseline_training_complete"


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


def _run(argv: list[str], *, cwd: Path, timeout_seconds: int) -> str:
    """workspace를 cwd로 subprocess를 실행하고 stdout만 돌려준다.

    실패 시 stderr를 예외에 싣지 않는다 — 학습 로그에 데이터 경로나 토큰이 섞여 들어올
    수 있어, 사유 코드만 남기고 본문은 Pod 로그로만 흘린다.
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
        raise TrainingError("training_timeout") from error
    except OSError as error:
        raise TrainingError("training_spawn_failed") from error
    if completed.returncode != 0:
        # 사유는 접미사 없는 고정 코드로 둔다. `phase2._safe_failure_reason`이
        # `^[a-z][a-z0-9_]*$`에 맞는 값만 로그에 남기고 나머지는 `redacted`로 지우므로
        # (#583), 인자·경로를 붙이면 오히려 사유가 통째로 사라진다. 실패한 명령의 상세는
        # container stdout/stderr로 흘러 Pod 로그에 남는다.
        raise TrainingError("training_command_failed")
    return completed.stdout


def resolve_policy_seeds(workspace: Path, *, timeout_seconds: int = 60) -> tuple[int, ...]:
    """workspace 코드가 선언한 `POLICY_SEEDS`를 그대로 읽는다.

    executor 이미지에 `src/`가 없으므로 import가 불가능하고, 상수를 복제하면 세 번째
    사본이 되어 드리프트 위험이 생긴다(이미 `issue_authoring.py`가 복제본이라 전용
    드리프트 테스트를 두고 있다). 대신 workspace의 코드에게 직접 물어본다.

    조건별로 다른 값이 나오는 것이 **정상**이다 — candidate가 seed 정책을 바꾸는
    실험이라면 candidate 학습은 바뀐 값으로 돌아야 한다.
    """
    raw = _run(
        ["python", "-c", _SEED_PROBE],
        cwd=workspace,
        timeout_seconds=timeout_seconds,
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
    _run(["uv", "sync"], cwd=workspace, timeout_seconds=timeout_seconds)


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
                "src.cli",
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
        )

    if config.stage is TrainingStage.BASELINE:
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(",".join(str(seed) for seed in seeds), encoding="utf-8")
    return seeds
