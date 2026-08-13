"""조건별 학습 산출물을 seed마다 채점해 하나의 실험 지표로 모으는 경계.

[파이프라인] `training.py`가 baseline·candidate 산출물을 남긴 뒤부터, finalizer가
결과를 게시·보고하기 전까지의 구간을 담당한다. 같은 Pod의 workspace에서
`autoresearch.cli evaluate-model`을 조건·seed마다 호출하고, 그 JSON을 실험 하나의
`metrics.json`으로 조립한다.

[기능] 조건별 seed 목록을 받아 held-out 지표를 수집하고, 같은 seed끼리 짝지은
delta와 그 평균·표준오차를 함께 싣는다. 두 조건이 같은 데이터·같은 테스트셋을 썼는지
확인할 수 있도록 테스트셋 지문도 남긴다. 그 전문에서 워크벤치에 실을 요약
(`experiment-metric-snapshot-v1`)을 뽑는 것도 이 모듈이 한다 — 요약이 전문과 다른
정의를 쓰지 않으려면 같은 곳에서 나와야 한다. 채점 subprocess가 실패하거나 timeout되면
어느 조건의 어느 seed였는지와 출력 tail을 컨테이너 로그로 남긴다(#636).

[비책임] 지표의 정의와 계산은 `autoresearch/model_evaluation/evaluate.py`가 소유한다 — 이 모듈은
호출과 조립만 한다. 학습은 `training.py`, GCS 게시와 API 보고는 finalizer,
가설의 성패 판정은 `report.md`를 읽는 사람과 리뷰 에이전트가 한다.

[중요] **판정하지 않는다.** 통계량을 계산해 싣지만 그것으로 통과·기각을 정하지
않는다. 고정 임계값 게이트를 승격 관문에서 뺀 것이 이 계약의 결정이며
(`docs/specs/2026-08-09-agent-authored-experiment-report.md`), 여기에 임계 비교를
되살리면 그 결정이 조용히 무효가 된다.
"""

from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
import logging
from pathlib import Path
import statistics
import subprocess
from typing import Final, cast

from agent_orchestration.executor.command_output import log_command_streams
from agent_orchestration.executor.training import TrainingStage


_LOGGER = logging.getLogger(__name__)

CONTRACT_VERSION: Final = "experiment-metrics-v1"

# 워크벤치에 싣는 요약의 계약이다. 전문(`experiment-metrics-v1`)과 버전을 따로 두는 이유는
# 요약이 화면 사정으로 바뀌어도 GCS에 남는 전문의 형태는 그대로여야 하기 때문이다.
SNAPSHOT_CONTRACT_VERSION: Final = "experiment-metric-snapshot-v1"

# 워크벤치가 "이 실험이 무엇을 올렸나"를 한 줄로 답할 때 쓰는 지표.
PRIMARY_METRIC_NAME: Final = "roc_auc"

# `evaluate-model`이 조건·seed마다 남기는 파일 이름. workspace 밖 산출물 루트에 둔다.
_METRICS_FILENAME: Final = "metrics_{seed}.json"

# 짝지어 비교할 주 지표. 나머지 지표도 전부 싣지만 delta는 이 셋만 계산한다 —
# 순위 지표와 캘리브레이션 지표를 함께 봐야 "주 지표만 오른 손상"이 드러난다.
PAIRED_METRIC_NAMES: Final = ("roc_auc", "log_loss", "brier")

_SHA256_CHUNK_BYTES: Final = 1024 * 1024


class MeasurementError(RuntimeError):
    """측정 단계 실패 사유다. 명령 출력과 자격 증명은 포함하지 않는다."""


@dataclass(frozen=True)
class MeasurementInput:
    """두 조건의 학습이 끝난 뒤 채점에 필요한 workspace 좌표와 산출물 루트."""

    workspace: Path
    output_root: Path
    seeds: tuple[int, ...]
    timeout_seconds: int

    def __post_init__(self) -> None:
        """채점을 시작할 수 없는 입력으로 subprocess를 띄우지 않게 막는다."""
        if not self.workspace.is_dir():
            raise MeasurementError("workspace_missing")
        if not self.output_root.is_dir():
            raise MeasurementError("training_output_missing")
        if not self.seeds:
            raise MeasurementError("seeds_missing")
        if not isinstance(self.timeout_seconds, int) or self.timeout_seconds <= 0:
            raise MeasurementError("timeout_invalid")


def _run(argv: list[str], *, cwd: Path, timeout_seconds: int, stage: str) -> None:
    """workspace를 cwd로 채점 subprocess를 실행한다.

    실패하면 출력 tail을 **사유와 분리된 로그 줄**로 남긴다(#636, `training._run`과 같은
    원칙). 사유에 붙이면 `phase2._safe_failure_reason`이 통째로 `redacted`로 지운다.

    `stage`는 호출 지점 이름이다. 채점은 조건 2 × seed 3으로 여섯 번 도는데 사유 코드는
    하나뿐이라, 어느 조건의 어느 seed가 죽었는지는 이 인자로만 남는다.
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
        # POSIX의 `subprocess.run`은 이미 수집한 출력을 이 예외에 실어 던진다(#636).
        _LOGGER.error(
            "evaluation command timed out stage=%s timeout_sec=%d",
            stage,
            timeout_seconds,
        )
        log_command_streams(
            _LOGGER,
            event="evaluation output",
            stage=stage,
            stdout=error.stdout,
            stderr=error.stderr,
        )
        raise MeasurementError("evaluation_timeout") from error
    except OSError as error:
        # 출력이 없는 실패라 tail로는 잡히지 않는다(#636, `training._run`과 같은 규칙).
        _LOGGER.error(
            "evaluation command could not start stage=%s error_type=%s reason=%s",
            stage,
            type(error).__name__,
            error.strerror,
        )
        raise MeasurementError("evaluation_spawn_failed") from error
    if completed.returncode != 0:
        _LOGGER.error(
            "evaluation command failed stage=%s exit_code=%d",
            stage,
            completed.returncode,
        )
        log_command_streams(
            _LOGGER,
            event="evaluation output",
            stage=stage,
            stdout=completed.stdout,
            stderr=completed.stderr,
        )
        # 사유는 접미사 없는 고정 코드로 둔다. `phase2._safe_failure_reason`이
        # `^[a-z][a-z0-9_]*$`에 맞는 값만 남기므로 인자를 붙이면 사유가 통째로 사라진다.
        raise MeasurementError("evaluation_command_failed")


def _sha256_file(path: Path) -> str:
    """테스트셋처럼 큰 파일도 한 번에 올리지 않고 지문을 만든다."""
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(_SHA256_CHUNK_BYTES):
                digest.update(chunk)
    except OSError as error:
        raise MeasurementError("test_set_unreadable") from error
    return digest.hexdigest()


def _condition_directory(output_root: Path, stage: TrainingStage) -> Path:
    directory = output_root / stage.value
    if not directory.is_dir():
        raise MeasurementError("condition_output_missing")
    return directory


def evaluate_condition(
    config: MeasurementInput, stage: TrainingStage
) -> dict[int, dict[str, object]]:
    """한 조건의 seed별 held-out 지표를 채점해 모은다.

    학습이 남긴 모델·테스트셋·피처 목록을 그대로 `evaluate-model`에 넘긴다. 지표
    정의를 여기서 다시 구현하지 않는 이유는 학습·평가·리포트가 같은 정의를 쓰게
    하기 위해서다 — 정의가 갈리면 리포트의 숫자가 파이프라인의 숫자와 달라진다.

    Returns:
        seed → `held-out-metrics-v1` payload 매핑.
    """
    directory = _condition_directory(config.output_root, stage)
    collected: dict[int, dict[str, object]] = {}
    for seed in config.seeds:
        model_path = directory / f"model_{seed}.txt"
        test_set_path = directory / f"test_{seed}.csv"
        feature_columns_path = directory / f"features_{seed}.json"
        for required in (model_path, test_set_path, feature_columns_path):
            if not required.is_file():
                raise MeasurementError("training_artifact_missing")
        metrics_path = directory / _METRICS_FILENAME.format(seed=seed)
        _run(
            [
                "python",
                "-m",
                "autoresearch.cli",
                "evaluate-model",
                "--data-path",
                str(test_set_path),
                "--model-path",
                str(model_path),
                "--feature-columns-path",
                str(feature_columns_path),
                "--metrics-output",
                str(metrics_path),
            ],
            cwd=config.workspace,
            timeout_seconds=config.timeout_seconds,
            stage=f"evaluate_model:{stage.value}:{seed}",
        )
        try:
            payload = json.loads(metrics_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            # `write_held_out_metrics`가 원자 게시하므로 여기 도달하면 파일이 아예
            # 안 생긴 것이지 반쯤 쓰인 것이 아니다.
            raise MeasurementError("metrics_unreadable") from error
        payload["test_set_sha256"] = _sha256_file(test_set_path)
        collected[seed] = payload
    return collected


def _paired_deltas(
    baseline: dict[int, dict[str, object]],
    candidate: dict[int, dict[str, object]],
    seeds: tuple[int, ...],
) -> dict[str, dict[str, object]]:
    """같은 seed끼리 짝지은 지표 차이와 그 요약을 만든다.

    짝을 지어 빼면 seed 자체가 만드는 변동(분할·초기화·샘플링)이 상쇄되고 코드 변경의
    효과만 남는다. 이것이 두 조건을 매번 같은 Pod에서 함께 학습하는 이유다.

    **판정하지 않는다** — 평균과 표준오차를 계산해 싣기만 한다. 신뢰구간을 여기서
    만들지 않는 이유는 t 임계값 표가 `autoresearch/model_evaluation/seed_sweep.py`에 있고, 그것을
    executor에 복제하면 두 벌이 갈라지기 때문이다. 필요한 쪽에서 원본을 쓴다.
    """
    summary: dict[str, dict[str, object]] = {}
    for name in PAIRED_METRIC_NAMES:
        deltas = {
            seed: float(candidate[seed][name]) - float(baseline[seed][name])
            for seed in seeds
        }
        values = list(deltas.values())
        summary[name] = {
            "per_seed": deltas,
            "mean": statistics.fmean(values),
            # seed가 하나뿐이면 표본 표준편차가 정의되지 않는다. 0으로 채우면 "변동이
            # 없다"로 읽히므로 계산하지 않았음을 null로 남긴다.
            "standard_error": (
                statistics.stdev(values) / (len(values) ** 0.5)
                if len(values) > 1
                else None
            ),
        }
    return summary


def build_experiment_metrics(
    config: MeasurementInput,
    *,
    coordinates: dict[str, object],
    dataset_fingerprint: str,
) -> dict[str, object]:
    """두 조건을 채점해 실험 하나의 지표 결과를 조립한다.

    Args:
        config: workspace·산출물 루트·seed 목록.
        coordinates: 실험 좌표(`experiment_id`, `issue_number`, `base_dev_sha`,
            `candidate_sha`, `image_digest` 등). 이 파일 하나만 보고 어느 실험의
            무엇인지 알 수 있어야 한다.
        dataset_fingerprint: 두 조건이 공유한 학습 스냅샷의 SHA-256.

    Returns:
        `experiment-metrics-v1` payload.
    """
    baseline = evaluate_condition(config, TrainingStage.BASELINE)
    candidate = evaluate_condition(config, TrainingStage.CANDIDATE)
    return {
        "contract_version": CONTRACT_VERSION,
        "coordinates": dict(coordinates),
        "dataset_fingerprint": dataset_fingerprint,
        "seeds": list(config.seeds),
        "conditions": {
            TrainingStage.BASELINE.value: {
                str(seed): payload for seed, payload in baseline.items()
            },
            TrainingStage.CANDIDATE.value: {
                str(seed): payload for seed, payload in candidate.items()
            },
        },
        "paired": _paired_deltas(baseline, candidate, config.seeds),
        # 두 조건의 테스트셋이 실제로 같은지 — 분할 코드도 candidate가 바꿀 수 있는
        # `src/**`라, 다르면 두 숫자는 애초에 비교 대상이 아니다. 지표는 멀쩡해 보이므로
        # 사실로 실어야 읽는 쪽이 "이 비교는 성립하지 않는다"를 말할 수 있다.
        "split_matches": {
            str(seed): baseline[seed]["test_set_sha256"]
            == candidate[seed]["test_set_sha256"]
            for seed in config.seeds
        },
    }


def build_metric_snapshot(
    metrics: dict[str, object], *, results_uri: str | None
) -> dict[str, object]:
    """전문에서 워크벤치가 한눈에 볼 요약을 뽑는다.

    전문을 그대로 실험 행에 싣지 않는 이유는 그것이 1초 polling으로 반복 조회되는
    값이기 때문이다. 조건 2 × seed 3의 전체 지표를 매번 실어 나르면 seed를 늘리는
    순간 목록 화면까지 느려진다. **전문은 GCS에 있고 이것은 그 입구다** —
    `results_uri`를 함께 싣는 이유가 그것이다.

    조건별 지표는 세 지표 모두의 seed 평균을 싣는다. 주 지표 하나만 실으면
    "주 지표는 올랐는데 캘리브레이션이 망가진" 변경을 요약만 보고는 알 수 없다.

    `split_matches`는 seed별 참·거짓을 전부 싣지 않고 **전부 참인지**만 싣는다.
    하나라도 거짓이면 그 비교는 애초에 성립하지 않으므로, 요약 단계에서 필요한 것은
    "이 숫자를 믿어도 되는가"라는 한 비트다. 어느 seed였는지는 전문이 답한다.

    Args:
        metrics: `build_experiment_metrics`가 만든 `experiment-metrics-v1` payload.
        results_uri: 전문을 게시한 위치. 게시하지 않는 배포에서는 `None`이다.

    Returns:
        `experiment-metric-snapshot-v1` payload.
    """
    conditions = cast(dict[str, dict[str, dict[str, object]]], metrics["conditions"])
    paired = cast(dict[str, dict[str, object]], metrics["paired"])
    split_matches = cast(dict[str, bool], metrics["split_matches"])
    return {
        "contract_version": SNAPSHOT_CONTRACT_VERSION,
        "primary_metric": PRIMARY_METRIC_NAME,
        "seeds": list(cast(list[int], metrics["seeds"])),
        "conditions": {
            stage: {
                name: statistics.fmean(
                    float(cast(float, per_seed[name]))
                    for per_seed in conditions[stage].values()
                )
                for name in PAIRED_METRIC_NAMES
            }
            for stage in (TrainingStage.BASELINE.value, TrainingStage.CANDIDATE.value)
        },
        "paired": {
            name: {
                "mean": paired[name]["mean"],
                "standard_error": paired[name]["standard_error"],
            }
            for name in PAIRED_METRIC_NAMES
        },
        "split_matches": all(split_matches.values()),
        "dataset_fingerprint": metrics["dataset_fingerprint"],
        "results_uri": results_uri,
    }


def write_experiment_metrics(payload: dict[str, object], destination: Path) -> Path:
    """조립한 실험 지표를 원자 게시한다.

    읽는 쪽이 "파일이 있으면 완결됐다"를 가정할 수 있어야 한다 —
    `evaluate.write_held_out_metrics`와 같은 계약이다.
    """
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_suffix(destination.suffix + ".tmp")
    try:
        temporary.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
            encoding="utf-8",
        )
        temporary.replace(destination)
    except OSError as error:
        temporary.unlink(missing_ok=True)
        raise MeasurementError("metrics_publish_failed") from error
    return destination
