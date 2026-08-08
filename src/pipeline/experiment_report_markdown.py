"""paired 판정 결과를 사람이 읽는 Markdown 리포트로 렌더링한다(#620).

[파이프라인] `compare-paired-experiment`가 판정을 마치고 결과 payload를 게시한 **뒤**
구간을 담당한다. 이미 내려진 판정을 표현만 하며, 사람이 승격 여부를 판단할 수 있는
형태로 옮긴다.

[기능] `paired-offline-experiment-result-v1`(`PairedExperimentResult`)을 입력으로 받아
판정·사유, before/after 지표와 신뢰구간, seed·분할 정보, 데이터·코드 provenance를
Markdown 문서로 만든다.

[비책임] 판정 자체(`experiment_evaluation`), 공정성 재검증(`training_comparison`),
결과 payload 생성(`paired_experiment`), Experiment API 반영(`experiment_result_report`)은
담당하지 않는다. 가설 텍스트 조달도 호출부의 몫이며 이 모듈은 인자로 받기만 한다.
"""

from __future__ import annotations

from typing import Final

from src.pipeline.paired_experiment import PairedExperimentResult


# 계획 Stage 5가 리포트에 요구하지만 `paired-offline-experiment-result-v1`이 아직 담지
# 않는 항목과, 그 데이터를 만들 단계다. 생략하지 않고 "미측정"으로 남기는 이유는
# 승격 판단자가 **없는 것과 0인 것을 구분**해야 하기 때문이다. 특히 기여 분해가 빠진
# 리포트는 2026-08-03 로컬 실증에서 실제로 났던 "피처 가설 성공" 오판을 그대로
# 반복시킨다 — 그때 모델만 +0.0577(유의)인데 피처만 +0.0043(노이즈)이었다.
_UNMEASURED_ITEMS: Final = (
    ("탐색 공간", "Stage 3"),
    ("우승 조합", "Stage 3"),
    ("기여 분해", "Stage 4"),
    ("보조 지표", "Stage 4"),
)


def _number(value: float) -> str:
    """지표를 지수 표기 없이 적는다. `:g`는 작은 delta를 `1e-05`로 바꿔 읽기 어렵다."""
    return f"{value:.6f}".rstrip("0").rstrip(".")


def _signed(value: float) -> str:
    """개선·악화를 부호로 즉시 읽게 한다."""
    return f"{value:+.6f}".rstrip("0").rstrip(".")


def _metric_lines(result: PairedExperimentResult) -> list[str]:
    """판정 지표를 렌더링하되, 없으면 빈 값이 아니라 사유를 남긴다.

    `comparison_failed`는 요청 검증이나 comparison 재검증에서 끊긴 경우라 판정 엔진이
    호출되지 않았고, 지표가 전부 `None`이다. 빈 표를 그리면 "측정했는데 0"으로 읽힌다.
    """
    if result.metric_name is None:
        return ["판정 지표 없음 — 판정 엔진에 도달하지 못했습니다."]

    lines = [f"- 주 지표: `{result.metric_name}`"]
    if result.primary_baseline is not None:
        lines.append(f"- baseline: {_number(result.primary_baseline)}")
    if result.primary_candidate is not None:
        lines.append(f"- candidate: {_number(result.primary_candidate)}")
    if result.paired_delta_mean is not None:
        lines.append(f"- paired 평균 차이: {_signed(result.paired_delta_mean)}")
    lower = result.confidence_interval_lower
    upper = result.confidence_interval_upper
    if lower is not None and upper is not None:
        lines.append(f"- 신뢰구간: [{_number(lower)}, {_number(upper)}]")
    return lines


def _seed_lines(result: PairedExperimentResult) -> list[str]:
    """seed 수와 분할 해시를 남긴다.

    seed 수가 보이지 않으면 기각 판정을 **"개선이 없었다"와 "검정력이 없었다"로 구분할
    수 없다.** 현행 3 seed는 자유도 2·t_critical≈4.30이라 신뢰구간이 극단적으로 넓어
    실제 개선도 기각으로 떨어진다(계획 Stage 1-4).
    """
    return [
        f"- seed: {len(result.seeds)}개 ({', '.join(str(s) for s in result.seeds)})",
        f"- split hash: `{result.split_hash}`",
    ]


def _provenance_lines(result: PairedExperimentResult) -> list[str]:
    """결론을 나중에 감사·재현할 좌표를 남긴다.

    2026-08-03 로컬 실증이 무효가 된 직접 원인이 **입력 CSV의 출처·기간·생성 명령
    기록이 없다**는 것이었다(spec §근거 ⑤). `dataset_fingerprint`는 content hash라
    "그때 그 데이터"를 바이트 단위로 특정한다.
    """
    return [
        f"- 학습 스냅샷: `{result.dataset_snapshot_uri}`",
        f"- 데이터 지문: `{result.dataset_fingerprint}`",
        f"- 학습 설정 지문: `{result.training_config_fingerprint}`",
        f"- baseline 코드: `{result.base_dev_sha}`",
        f"- candidate 코드: `{result.candidate_sha}`",
    ]


def render_experiment_report(
    result: PairedExperimentResult, *, hypothesis: str | None = None
) -> str:
    """판정 결과 하나를 Markdown 문서로 만든다.

    `hypothesis`는 `paired-offline-experiment-result-v1`에 없는 값이라 호출부가 조달해
    넘긴다. 이 모듈은 네트워크를 타지 않으므로 Experiment API에서 읽어오지 않는다.
    """
    lines = [
        "## 가설",
        "",
        hypothesis if hypothesis else "가설 없음 — 호출부가 전달하지 않았습니다.",
        "",
        "## 판정",
        "",
        f"- 결과: `{result.outcome}`",
        f"- 사유: `{result.decision_reason}`",
        "",
        "## 지표",
        "",
        *_metric_lines(result),
        "",
        "## seed·분할",
        "",
        *_seed_lines(result),
        "",
        "## provenance",
        "",
        *_provenance_lines(result),
        "",
        "## 미측정 항목",
        "",
    ]
    lines.extend(f"- {item}: 미측정 — {stage}" for item, stage in _UNMEASURED_ITEMS)
    lines.append("")
    return "\n".join(lines)
