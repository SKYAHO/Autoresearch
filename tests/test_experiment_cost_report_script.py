"""원가 집계 스크립트가 정본 파서로 사용량을 읽는지 검증한다.

전체 파이프라인에서 executor가 남긴 로그를 실험당 원가로 바꾸는 분석 도구 구간을
검증한다. 파싱 자체의 계약은 `tests/test_experiment_cost_api.py`가 담당한다.

**이 파일이 막는 것은 조용한 0건이다.** 사용량 줄 형식은 executor가 소유하는 하나의
계약인데, 그것을 읽는 코드가 둘로 갈리면 한쪽만 고쳐도 오류 없이 "기록이 없다"로
보인다. 값이 틀리는 것보다 발견이 늦다(#746).
"""

from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest

from agent_orchestration.app.experiments.cost import (
    TOKEN_PRICE_INPUT_USD,
    parse_stage_tokens,
)


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "experiment_cost_report.py"
_spec = importlib.util.spec_from_file_location("experiment_cost_report", _SCRIPT)
report = importlib.util.module_from_spec(_spec)
# dataclass 처리가 `sys.modules[cls.__module__]`를 조회하므로 exec 전에 등록한다.
sys.modules["experiment_cost_report"] = report
_spec.loader.exec_module(report)


def _usage_line(stage: str, *, input_tokens: int, cached: int, output: int) -> str:
    """executor가 남기는 구조화 사용량 줄을 그대로 만든다(#742)."""
    return (
        f"codex token usage stage={stage} available=1 turns=1 "
        f"input={input_tokens} cached_input={cached} "
        f"fresh_input={input_tokens - cached} output={output} "
        f"reasoning=0 total={input_tokens + output}\n"
    )


def test_script_reads_the_same_usage_lines_as_the_api() -> None:
    """스크립트와 API가 같은 줄에서 같은 값을 읽어야 한다."""
    line = _usage_line("codex-worker", input_tokens=94_393, cached=84_954, output=4_968)
    logs = [{"id": "exp-1", "stage": "codex-worker", "content": line}]

    collected = report.collect_usage(logs, output_ratio=0.05, cache_ratio=0.9)
    canonical = parse_stage_tokens([line])[0]

    usage = collected["exp-1"]["codex-worker"]
    assert usage.exact is True
    assert usage.input_tokens == canonical.input_tokens
    assert usage.cached_input_tokens == canonical.cached_input_tokens
    assert usage.output_tokens == canonical.output_tokens


def test_structured_lines_win_over_the_legacy_total_for_the_same_experiment() -> None:
    """같은 실행을 두 출처에서 세면 두 배가 된다 — 구조화 줄이 있으면 총량은 버린다."""
    content = (
        _usage_line("codex-worker", input_tokens=1000, cached=900, output=50)
        + "tokens used\n1,050\n"
    )
    logs = [{"id": "exp-1", "stage": "codex-worker", "content": content}]

    collected = report.collect_usage(logs, output_ratio=0.05, cache_ratio=0.9)

    stages = collected["exp-1"]
    assert list(stages) == ["codex-worker"]
    assert stages["codex-worker"].input_tokens == 1000


def test_legacy_only_experiment_is_split_by_the_assumed_ratios() -> None:
    """총량뿐인 과거 실험의 가정 분해는 화면이 아니라 이 도구가 한다(#744 결정)."""
    logs = [{"id": "exp-1", "stage": "codex-worker", "content": "tokens used\n1,000\n"}]

    collected = report.collect_usage(logs, output_ratio=0.1, cache_ratio=0.5)

    usage = collected["exp-1"]["codex-worker"]
    assert usage.exact is False
    assert usage.output_tokens == 100
    assert usage.input_tokens == 900
    assert usage.cached_input_tokens == 450


def test_default_token_prices_come_from_the_canonical_module() -> None:
    """단가도 두 곳에 두면 같은 이유로 갈린다."""
    args = report.parse_args([])

    assert args.token_price_input == TOKEN_PRICE_INPUT_USD


def test_script_defines_no_usage_pattern_of_its_own() -> None:
    """사용량 줄 형식이 저장소에 두 벌 생기는 것을 막는다."""
    source = _SCRIPT.read_text(encoding="utf-8")

    assert "codex token usage stage=" not in source


@pytest.mark.parametrize("attribute", ("parse_stage_tokens", "parse_legacy_total_tokens"))
def test_script_uses_the_canonical_parsers(attribute: str) -> None:
    """파서를 import해 쓰는지 확인한다 — 복사해 오면 이 테스트가 통과해도 위 테스트가 막는다."""
    assert hasattr(report, attribute)
