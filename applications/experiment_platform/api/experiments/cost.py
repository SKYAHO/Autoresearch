"""실험 한 건의 실행 비용을 기존 기록에서 파생한다.

전체 파이프라인에서 executor가 남긴 로그와 시각 기록을 "이 실험이 얼마나 걸렸고
얼마어치를 썼는가"로 바꾸는 순수 계산 구간을 담당한다. DB 조회와 HTTP 변환은
`repository.py`·`router.py`가, 화면 배치는 워크벤치가 담당한다.

Codex 토큰 사용량은 executor가 남기는 구조화 줄(#742)에서 stage별로 읽는다. 그 줄이
없는 과거 실험은 총량 한 줄(`tokens used`)만 남아 있어 총량만 돌려주고 분해 없음을
표시한다. **가정 비율로 쪼개지 않는다** — 화면에 뜨는 숫자는 측정값이어야 하고,
추정은 분석 스크립트(`scripts/experiment_cost_report.py`)의 몫이다.

[중요] 단가는 배포 시점의 외부 사실이라 여기 상수로 둔다. 환경 변수로 받지 않는
이유는 값이 없을 때 화면이 죽거나 0원을 보이는 편이 더 나쁘기 때문이다. 단가가
바뀌면 이 파일을 고친다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final


# executor가 남기는 구조화 사용량 줄(#742). 형식이 바뀌면 이 정규식도 같은 PR에서 바꾼다.
_USAGE_LINE_PATTERN: Final = re.compile(
    r"codex token usage stage=(?P<stage>[a-z0-9-]+) available=1 turns=(?P<turns>\d+) "
    r"input=(?P<input>\d+) cached_input=(?P<cached>\d+) fresh_input=\d+ "
    r"output=(?P<output>\d+) reasoning=(?P<reasoning>\d+) total=\d+"
)
# #742 이전 실험의 유일한 흔적. Codex stdout이 싣던 총량 한 줄이다.
_LEGACY_TOTAL_PATTERN: Final = re.compile(r"tokens used\s*\n\s*([\d,]+)")

# `gpt-5.6-luna` 표준 단가(USD / 1M tokens).
# 출처: https://developers.openai.com/api/docs/pricing (2026-08 확인)
TOKEN_PRICE_INPUT_USD: Final = 0.20
TOKEN_PRICE_CACHED_USD: Final = 0.02
TOKEN_PRICE_OUTPUT_USD: Final = 1.20

# executor Pod가 점유하는 몫의 시간당 단가(USD). `batch-od` 노드풀(e2-standard-8,
# asia-northeast3 OnDemand)의 실측 vCPU/RAM 단가에 Job의 container request
# (1 vCPU / 2 GiB, `launcher.jobs._container_resources`)를 곱한 값이다.
# 노드를 전유했다고 보는 계산이 아니라 **한계 비용**이다 — 실험 하나를 더 돌릴 때
# 늘어나는 몫이 사용자가 화면에서 알고 싶은 값이다.
COMPUTE_HOURLY_USD: Final = 0.03550464


@dataclass(frozen=True)
class StageTokens:
    """Codex를 실행한 stage 하나의 토큰 사용량."""

    stage: str
    input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    reasoning_output_tokens: int

    @property
    def fresh_input_tokens(self) -> int:
        """캐시에 걸리지 않아 정가로 과금되는 입력 토큰."""
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def total_tokens(self) -> int:
        """입력과 출력을 합한 총량."""
        return self.input_tokens + self.output_tokens


@dataclass(frozen=True)
class ExperimentCost:
    """실험 한 건의 실행 비용.

    `breakdown_available`이 거짓이면 `stages`는 비어 있고 `total_tokens`만 의미가 있다.
    이때 `token_usd`는 `None`이다 — 분해 없이는 캐시 적중분을 뗄 수 없고, 정가로 매기면
    실제보다 몇 배 큰 금액을 사실처럼 보이게 한다.
    """

    wall_clock_seconds: float | None
    compute_usd: float | None
    breakdown_available: bool
    stages: tuple[StageTokens, ...]
    total_tokens: int
    token_usd: float | None
    token_usd_without_cache: float | None


def parse_stage_tokens(log_contents: list[str]) -> tuple[StageTokens, ...]:
    """로그 본문에서 stage별 토큰 사용량을 모은다.

    같은 stage가 여러 줄에 걸쳐 나오면 합산한다. 로그 수집기가 8000자 청크로 쪼개
    적재하므로(#559) 호출자는 실험 하나의 본문을 이어 붙여 넘겨야 한다.
    """
    totals: dict[str, list[int]] = {}
    for content in log_contents:
        for match in _USAGE_LINE_PATTERN.finditer(content):
            bucket = totals.setdefault(match.group("stage"), [0, 0, 0, 0])
            bucket[0] += int(match.group("input"))
            bucket[1] += int(match.group("cached"))
            bucket[2] += int(match.group("output"))
            bucket[3] += int(match.group("reasoning"))
    return tuple(
        StageTokens(
            stage=stage,
            input_tokens=values[0],
            cached_input_tokens=values[1],
            output_tokens=values[2],
            reasoning_output_tokens=values[3],
        )
        for stage, values in sorted(totals.items())
    )


def parse_legacy_total_tokens(log_contents: list[str]) -> int:
    """구조화 줄이 없던 시절의 총량 합계를 읽는다."""
    total = 0
    for content in log_contents:
        for match in _LEGACY_TOTAL_PATTERN.finditer(content):
            total += int(match.group(1).replace(",", ""))
    return total


def _price_tokens(stages: tuple[StageTokens, ...], *, with_cache: bool) -> float:
    """stage 전체의 종량제 환산액을 계산한다."""
    total = 0.0
    for stage in stages:
        if with_cache:
            total += (
                stage.fresh_input_tokens * TOKEN_PRICE_INPUT_USD
                + stage.cached_input_tokens * TOKEN_PRICE_CACHED_USD
            )
        else:
            total += stage.input_tokens * TOKEN_PRICE_INPUT_USD
        total += stage.output_tokens * TOKEN_PRICE_OUTPUT_USD
    return total / 1_000_000


def build_experiment_cost(
    *, wall_clock_seconds: float | None, log_contents: list[str]
) -> ExperimentCost:
    """벽시계와 로그 본문으로 실행 비용 하나를 조립한다.

    벽시계가 없으면(아직 Job이 뜨지 않은 실험) 컴퓨트 금액도 `None`이다. 0으로 채우면
    "안 들었다"로 읽히는데, 사실은 "아직 모른다"이다.
    """
    stages = parse_stage_tokens(log_contents)
    compute_usd = (
        wall_clock_seconds / 3600 * COMPUTE_HOURLY_USD
        if wall_clock_seconds is not None
        else None
    )
    if stages:
        return ExperimentCost(
            wall_clock_seconds=wall_clock_seconds,
            compute_usd=compute_usd,
            breakdown_available=True,
            stages=stages,
            total_tokens=sum(stage.total_tokens for stage in stages),
            token_usd=_price_tokens(stages, with_cache=True),
            token_usd_without_cache=_price_tokens(stages, with_cache=False),
        )
    return ExperimentCost(
        wall_clock_seconds=wall_clock_seconds,
        compute_usd=compute_usd,
        breakdown_available=False,
        stages=(),
        total_tokens=parse_legacy_total_tokens(log_contents),
        token_usd=None,
        token_usd_without_cache=None,
    )
