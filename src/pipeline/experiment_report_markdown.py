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


def _metric_lines(result: PairedExperimentResult) -> list[str]:
    """판정 지표를 렌더링하되, 없으면 빈 값이 아니라 사유를 남긴다.

    `comparison_failed`는 요청 검증이나 comparison 재검증에서 끊긴 경우라 판정 엔진이
    호출되지 않았고, 지표가 전부 `None`이다. 빈 표를 그리면 "측정했는데 0"으로 읽힌다.
    """
    if result.metric_name is None:
        return ["판정 지표 없음 — 판정 엔진에 도달하지 못했습니다."]
    return []


def render_experiment_report(result: PairedExperimentResult) -> str:
    """판정 결과 하나를 Markdown 문서로 만든다."""
    lines = [
        "## 판정",
        "",
        f"- 결과: `{result.outcome}`",
        f"- 사유: `{result.decision_reason}`",
        "",
        "## 지표",
        "",
        *_metric_lines(result),
        "",
        "## 미측정 항목",
        "",
    ]
    lines.extend(f"- {item}: 미측정 — {stage}" for item, stage in _UNMEASURED_ITEMS)
    lines.append("")
    return "\n".join(lines)
