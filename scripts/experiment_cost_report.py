"""실험 한 건의 실제 원가(컴퓨트)와 구독제 Codex의 종량제 환산 절약액을 집계한다.

[파이프라인]
executor Job이 끝나 `experiments`·`experiment_events`·`experiment_logs`에 기록이 쌓인
뒤부터, 그 기록을 실험당 비용 수치로 바꾸는 관측 구간을 담당한다. 실험 실행에는
관여하지 않는 읽기 전용 스크립트다.

[기능]
① Agent Orchestration API Pod 안에서 읽기 전용 질의를 실행해(자격 증명을 로컬로
꺼내지 않는다) 실험별 벽시계 시간과 Codex 토큰 사용량을 모은다. 사용량은 executor가
남기는 구조화 줄(`codex token usage ...`, #742)에서 input·cached·output 분해로 읽고,
그 줄이 없는 과거 실험은 총량 한 줄(`tokens used`)로 되돌아간다.
② GCP Cloud Billing Catalog API에서 노드 vCPU·RAM 시간 단가를 실측해 실험당
컴퓨트 원가를 한계 기준(Pod request 점유)과 노드 기준(노드 전유) 두 가지로 낸다.
③ 토큰 단가를 적용해 "API 종량제였다면" 지출했을 금액과 프롬프트 캐싱이 덜어 준
금액을 stage별로 환산한다. 현재 Codex는 구독 인증이라 이 금액 전부가 절약액이다.

[비책임]
실험 실행·상태 전이(`applications.experiment_platform`), 지표 계산(`autoresearch/model_evaluation/evaluate.py`),
노드풀·청구 계정 설정(`SKYAHO/Autoresearch-infra`)은 담당하지 않는다.

사용 예:

    uv run --no-sync python scripts/experiment_cost_report.py --usd-krw 1380
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, replace
import json
import re
import shutil
import statistics
import subprocess
import sys
from typing import Any, Final
import urllib.request


COMPUTE_ENGINE_SERVICE: Final = "services/6F81-5844-456A"
BILLING_SKU_URL: Final = (
    "https://cloudbilling.googleapis.com/v1/"
    f"{COMPUTE_ENGINE_SERVICE}/skus?currencyCode=USD&pageSize=5000"
)

# executor가 남기는 구조화 사용량 줄(#742). 이 줄이 있으면 가정 없이 과금 구분이 나온다.
USAGE_LINE_PATTERN: Final = re.compile(
    r"codex token usage stage=(?P<stage>\S+) available=1 turns=(?P<turns>\d+) "
    r"input=(?P<input>\d+) cached_input=(?P<cached>\d+) fresh_input=(?P<fresh>\d+) "
    r"output=(?P<output>\d+) reasoning=(?P<reasoning>\d+) total=(?P<total>\d+)"
)
# #742 이전 실험의 유일한 흔적. 총량 하나뿐이라 분해는 가정에 기댄다.
LEGACY_TOTAL_PATTERN: Final = re.compile(r"tokens used\s*\n\s*([\d,]+)")

# `gpt-5.6-luna` 표준 단가(USD / 1M tokens, 2026-08 기준).
# 출처: https://developers.openai.com/api/docs/pricing — 모델이나 단가가 바뀌면 인자로 덮는다.
DEFAULT_MODEL: Final = "gpt-5.6-luna"
DEFAULT_PRICE_INPUT: Final = 0.20
DEFAULT_PRICE_CACHED: Final = 0.02
DEFAULT_PRICE_OUTPUT: Final = 1.20

# machine type 접미사별 (vCPU, GiB). 노드풀이 바뀌면 `--node-vcpu`/`--node-memory-gib`로
# 덮어쓴다 — 표를 늘리는 것보다 인자로 받는 편이 드리프트가 없다.
MACHINE_SHAPES: Final[dict[str, tuple[int, float]]] = {
    "e2-standard-8": (8, 32.0),
    "e2-standard-4": (4, 16.0),
    "e2-standard-2": (2, 8.0),
    "n2-standard-2": (2, 8.0),
    "n2-highmem-4": (4, 32.0),
}

# Pod 안에서 실행되는 읽기 전용 질의. entrypoint와 같은 방식으로 `db.env`를 읽어
# 자격 증명이 로컬 셸이나 이 스크립트의 프로세스 환경에 들어오지 않게 한다.
IN_POD_PROBE: Final = '''
import json, os, pathlib
line = pathlib.Path(os.environ["ORCH_RUNTIME_DIR"], "db.env").read_text().splitlines()[0]
url = line.split("=", 1)[1].replace("postgresql://", "postgresql+psycopg://", 1)
from sqlalchemy import create_engine, text

engine = create_engine(url)
with engine.connect() as conn:
    durations = conn.execute(text("""
        SELECT x.id, x.status,
               EXTRACT(EPOCH FROM (e.last_at - x.executor_job_created_at)) AS seconds
        FROM experiments x
        JOIN LATERAL (
            SELECT max(created_at) AS last_at
            FROM experiment_events ev WHERE ev.experiment_id = x.id
        ) e ON true
        WHERE x.executor_job_created_at IS NOT NULL AND e.last_at IS NOT NULL
    """)).all()
    logs = conn.execute(text("""
        SELECT experiment_id, log_type, string_agg(content, '' ORDER BY created_at) AS joined
        FROM experiment_logs GROUP BY experiment_id, log_type
    """)).all()

print(json.dumps({
    "durations": [
        {"id": str(r[0]), "status": r[1], "seconds": float(r[2])} for r in durations
    ],
    "logs": [
        {"id": str(r[0]), "stage": r[1], "content": r[2] or ""} for r in logs
    ],
}))
'''


@dataclass(frozen=True)
class NodePrice:
    """노드 한 대와 Pod request 한 몫의 시간당 USD 단가."""

    vcpu_hour: float
    gib_hour: float
    node_hour: float
    pod_hour: float


@dataclass(frozen=True)
class TokenUsage:
    """Codex 실행 한 묶음의 토큰 사용량.

    `exact`가 참이면 executor가 남긴 과금 구분을 그대로 읽은 값이고, 거짓이면 총량만
    남은 과거 실험을 가정 비율로 쪼갠 값이다. 두 표본을 섞어 평균 내면 정확도 주장이
    조용히 무너지므로 이 구분을 끝까지 들고 다닌다.
    """

    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    exact: bool = True

    @property
    def fresh_input_tokens(self) -> int:
        """캐시에 걸리지 않아 정가로 과금되는 입력 토큰."""
        return max(0, self.input_tokens - self.cached_input_tokens)

    @property
    def total_tokens(self) -> int:
        """입력과 출력을 합한 총량."""
        return self.input_tokens + self.output_tokens

    def merged(self, other: "TokenUsage") -> "TokenUsage":
        """같은 실험의 다른 stage 사용량을 합친다."""
        return TokenUsage(
            input_tokens=self.input_tokens + other.input_tokens,
            cached_input_tokens=self.cached_input_tokens + other.cached_input_tokens,
            output_tokens=self.output_tokens + other.output_tokens,
            reasoning_output_tokens=(
                self.reasoning_output_tokens + other.reasoning_output_tokens
            ),
            exact=self.exact and other.exact,
        )


@dataclass(frozen=True)
class TokenPrice:
    """1M 토큰당 USD 단가."""

    input_usd: float
    cached_usd: float
    output_usd: float

    def bill(self, usage: TokenUsage) -> float:
        """캐시 적중분을 따로 매겨 실제 청구액을 계산한다."""
        return (
            usage.fresh_input_tokens * self.input_usd
            + usage.cached_input_tokens * self.cached_usd
            + usage.output_tokens * self.output_usd
        ) / 1_000_000

    def bill_without_cache(self, usage: TokenUsage) -> float:
        """같은 토큰을 캐시 할인 없이 매겼을 때의 금액."""
        return (
            usage.input_tokens * self.input_usd + usage.output_tokens * self.output_usd
        ) / 1_000_000


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    """명령행 인자를 해석한다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--namespace", default="autoresearch")
    parser.add_argument(
        "--pod-selector", default="app.kubernetes.io/name=agent-orchestration-api"
    )
    parser.add_argument("--container", default="api")
    parser.add_argument("--region", default="asia-northeast3")
    parser.add_argument("--machine-type", default="e2-standard-8")
    parser.add_argument("--node-vcpu", type=int, default=None)
    parser.add_argument("--node-memory-gib", type=float, default=None)
    parser.add_argument(
        "--pod-cpu",
        type=float,
        default=1.0,
        help="executor container의 CPU request (launcher.jobs._container_resources)",
    )
    parser.add_argument("--pod-memory-gib", type=float, default=2.0)
    parser.add_argument(
        "--status",
        default="PASSED",
        help="원가 집계 대상 상태. ERROR는 회수 시각이 실행 시간과 무관해 기본에서 뺀다",
    )
    parser.add_argument("--model", default=DEFAULT_MODEL, help="단가 표기에 쓰는 모델 이름")
    parser.add_argument("--token-price-input", type=float, default=DEFAULT_PRICE_INPUT)
    parser.add_argument("--token-price-cached", type=float, default=DEFAULT_PRICE_CACHED)
    parser.add_argument("--token-price-output", type=float, default=DEFAULT_PRICE_OUTPUT)
    parser.add_argument(
        "--legacy-output-ratio",
        type=float,
        default=0.05,
        help="구조화 줄이 없는 과거 실험에만 적용하는 output 비율 가정",
    )
    parser.add_argument(
        "--legacy-cache-ratio",
        type=float,
        default=0.9,
        help="구조화 줄이 없는 과거 실험에만 적용하는 캐시 적중 비율 가정",
    )
    parser.add_argument("--usd-krw", type=float, default=None, help="원화 환산 환율")
    return parser.parse_args(argv)


def resolve_pod(namespace: str, selector: str) -> str:
    """selector에 걸리는 Running Pod 이름 하나를 고른다."""
    if shutil.which("kubectl") is None:
        raise SystemExit("kubectl을 찾을 수 없습니다.")
    completed = subprocess.run(
        [
            "kubectl", "-n", namespace, "get", "pod",
            "-l", selector, "--field-selector=status.phase=Running",
            "-o", "jsonpath={.items[0].metadata.name}",
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    name = completed.stdout.strip()
    if completed.returncode != 0 or not name:
        raise SystemExit(f"Pod을 찾지 못했습니다: {completed.stderr.strip()[:200]}")
    return name


def fetch_experiment_records(namespace: str, pod: str, container: str) -> dict[str, Any]:
    """API Pod 안에서 읽기 전용 질의를 실행하고 결과 JSON을 돌려받는다."""
    completed = subprocess.run(
        ["kubectl", "-n", namespace, "exec", "-i", pod, "-c", container, "--", "python", "-"],
        input=IN_POD_PROBE,
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit(f"Pod 질의 실패: {completed.stderr.strip()[-500:]}")
    return json.loads(completed.stdout.strip().splitlines()[-1])


def _access_token() -> str:
    """gcloud가 들고 있는 액세스 토큰을 얻는다."""
    completed = subprocess.run(
        ["gcloud", "auth", "print-access-token"],
        capture_output=True,
        text=True,
        check=False,
    )
    if completed.returncode != 0:
        raise SystemExit("gcloud 액세스 토큰을 얻지 못했습니다. `gcloud auth login`이 필요합니다.")
    return completed.stdout.strip()


def fetch_node_price(
    region: str, machine_type: str, vcpu: int, memory_gib: float,
    pod_cpu: float, pod_memory_gib: float,
) -> NodePrice:
    """Cloud Billing Catalog에서 해당 리전의 OnDemand vCPU·RAM 단가를 읽는다."""
    family = machine_type.split("-", 1)[0].upper()
    token = _access_token()
    core_price: float | None = None
    ram_price: float | None = None
    page = ""
    for _ in range(30):
        url = BILLING_SKU_URL + (f"&pageToken={page}" if page else "")
        request = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}"})
        with urllib.request.urlopen(request) as response:  # noqa: S310 - 고정 호스트
            payload = json.load(response)
        for sku in payload.get("skus", []):
            if region not in sku.get("serviceRegions", []):
                continue
            if sku["category"].get("usageType") != "OnDemand":
                continue
            description = sku["description"]
            if not description.startswith(f"{family} Instance "):
                continue
            expression = sku["pricingInfo"][0]["pricingExpression"]
            unit = expression["tieredRates"][-1]["unitPrice"]
            price = float(unit.get("units", 0)) + unit.get("nanos", 0) / 1e9
            if "Instance Core" in description:
                core_price = price
            elif "Instance Ram" in description:
                ram_price = price
        page = payload.get("nextPageToken", "")
        if not page:
            break
    if core_price is None or ram_price is None:
        raise SystemExit(f"{region}의 {family} 단가를 찾지 못했습니다.")
    return NodePrice(
        vcpu_hour=core_price,
        gib_hour=ram_price,
        node_hour=core_price * vcpu + ram_price * memory_gib,
        pod_hour=core_price * pod_cpu + ram_price * pod_memory_gib,
    )


def collect_usage(
    logs: list[dict[str, Any]], *, output_ratio: float, cache_ratio: float
) -> dict[str, dict[str, TokenUsage]]:
    """실험별·stage별 토큰 사용량을 모은다.

    구조화 줄이 하나라도 있으면 그 실험은 총량 줄을 무시한다 — 같은 실행을 두 출처에서
    세면 두 배가 된다. 로그 수집기가 8000자 청크로 쪼개 적재하므로 질의에서 이어 붙인
    뒤 찾는다.
    """
    exact: dict[str, dict[str, TokenUsage]] = {}
    legacy: dict[str, dict[str, TokenUsage]] = {}
    for row in logs:
        experiment_id, container = row["id"], row["stage"]
        for match in USAGE_LINE_PATTERN.finditer(row["content"]):
            usage = TokenUsage(
                input_tokens=int(match.group("input")),
                cached_input_tokens=int(match.group("cached")),
                output_tokens=int(match.group("output")),
                reasoning_output_tokens=int(match.group("reasoning")),
                exact=True,
            )
            stages = exact.setdefault(experiment_id, {})
            stage = match.group("stage")
            stages[stage] = stages.get(stage, TokenUsage()).merged(usage)
        for match in LEGACY_TOTAL_PATTERN.finditer(row["content"]):
            total = int(match.group(1).replace(",", ""))
            output_tokens = round(total * output_ratio)
            input_tokens = total - output_tokens
            usage = TokenUsage(
                input_tokens=input_tokens,
                cached_input_tokens=round(input_tokens * cache_ratio),
                output_tokens=output_tokens,
                exact=False,
            )
            stages = legacy.setdefault(experiment_id, {})
            stages[container] = stages.get(container, TokenUsage(exact=False)).merged(usage)
    return {**{k: v for k, v in legacy.items() if k not in exact}, **exact}


def _summarize(values: list[float]) -> dict[str, float]:
    """표본의 대표값을 만든다."""
    return {
        "n": len(values),
        "mean": statistics.mean(values),
        "median": statistics.median(values),
        "min": min(values),
        "max": max(values),
    }


def _mean_usage(samples: list[TokenUsage]) -> TokenUsage:
    """표본 평균을 하나의 사용량으로 만든다."""
    count = len(samples)
    total = TokenUsage(exact=all(sample.exact for sample in samples))
    for sample in samples:
        total = total.merged(replace(sample, exact=total.exact))
    return TokenUsage(
        input_tokens=round(total.input_tokens / count),
        cached_input_tokens=round(total.cached_input_tokens / count),
        output_tokens=round(total.output_tokens / count),
        reasoning_output_tokens=round(total.reasoning_output_tokens / count),
        exact=total.exact,
    )


def _print_compute(args: argparse.Namespace, price: NodePrice, minutes: dict[str, float]) -> None:
    """컴퓨트 원가 절을 출력한다."""
    hours = minutes["mean"] / 60
    marginal = hours * price.pod_hour
    node_share = hours * price.node_hour
    print("## 컴퓨트 원가 (실험 1건, 평균 기준)")
    print(f"  한계 기준(Pod request 점유) : ${marginal:.4f}")
    print(f"  노드 기준(노드 전유)        : ${node_share:.4f}")
    if args.usd_krw:
        print(
            f"  원화 환산                   : {marginal * args.usd_krw:,.0f}원"
            f" ~ {node_share * args.usd_krw:,.0f}원"
        )
    print("  ※ 디스크·네트워크·MLflow/GCS는 제외했습니다.\n")


def _print_tokens(
    args: argparse.Namespace,
    price: TokenPrice,
    per_experiment: dict[str, dict[str, TokenUsage]],
) -> None:
    """stage별 토큰과 종량제 환산액을 출력한다."""
    stage_names = sorted({stage for stages in per_experiment.values() for stage in stages})
    totals = [
        _fold(stages.values())
        for stages in per_experiment.values()
    ]
    exact_count = sum(1 for usage in totals if usage.exact)

    print(f"## Codex 토큰 ({len(totals)}건 중 실측 분해 {exact_count}건)")
    print(f"  단가({args.model}) : input ${price.input_usd}/1M, "
          f"cached ${price.cached_usd}/1M, output ${price.output_usd}/1M\n")

    header = f"  {'stage':<20}{'input':>12}{'cached':>12}{'fresh':>12}{'output':>10}{'USD':>10}"
    print(header)
    print("  " + "-" * (len(header) - 2))
    for stage in stage_names:
        samples = [stages[stage] for stages in per_experiment.values() if stage in stages]
        mean = _mean_usage(samples)
        print(
            f"  {stage:<20}{mean.input_tokens:>12,}{mean.cached_input_tokens:>12,}"
            f"{mean.fresh_input_tokens:>12,}{mean.output_tokens:>10,}"
            f"{price.bill(mean):>10.4f}"
        )
    mean_total = _mean_usage(totals)
    print(
        f"  {'합계':<19}{mean_total.input_tokens:>12,}{mean_total.cached_input_tokens:>12,}"
        f"{mean_total.fresh_input_tokens:>12,}{mean_total.output_tokens:>10,}"
        f"{price.bill(mean_total):>10.4f}"
    )

    billed = price.bill(mean_total)
    without_cache = price.bill_without_cache(mean_total)
    cache_hit = (
        mean_total.cached_input_tokens / mean_total.input_tokens
        if mean_total.input_tokens
        else 0.0
    )
    print(f"\n## 종량제 환산 (실험 1건 평균, 캐시 적중률 {cache_hit:.1%})")
    print(f"  캐싱 적용 환산액 : ${billed:.4f}")
    print(f"  캐싱 없을 때     : ${without_cache:.4f}")
    if without_cache:
        print(
            f"  캐싱 절약분      : ${without_cache - billed:.4f}"
            f" ({1 - billed / without_cache:.0%})"
        )
    print(f"  구독제 실제 지출 : $0 → 실험당 ${billed:.4f} 전액이 절약액")
    print(f"  {len(totals)}건 누적 절약액 : ${billed * len(totals):.2f}")
    if args.usd_krw:
        print(f"  원화 환산 누적   : {billed * len(totals) * args.usd_krw:,.0f}원")
    if exact_count < len(totals):
        print(
            f"\n  ※ {len(totals) - exact_count}건은 #742 이전 실험이라 총량만 남아 있어"
            f" output {args.legacy_output_ratio:.0%}·캐시 {args.legacy_cache_ratio:.0%}"
            " 가정으로 쪼갰습니다."
        )


def _fold(usages: Any) -> TokenUsage:
    """여러 stage의 사용량을 하나로 접는다."""
    folded = TokenUsage(exact=True)
    for usage in usages:
        folded = folded.merged(usage)
    return folded


def main(argv: list[str] | None = None) -> int:
    """집계 결과를 표준 출력에 적는다."""
    args = parse_args(argv)
    shape = MACHINE_SHAPES.get(args.machine_type)
    vcpu = args.node_vcpu or (shape[0] if shape else None)
    memory_gib = args.node_memory_gib or (shape[1] if shape else None)
    if vcpu is None or memory_gib is None:
        raise SystemExit(
            f"{args.machine_type}의 형상을 모릅니다. --node-vcpu/--node-memory-gib를 주십시오."
        )

    pod = resolve_pod(args.namespace, args.pod_selector)
    records = fetch_experiment_records(args.namespace, pod, args.container)
    price = fetch_node_price(
        args.region, args.machine_type, vcpu, memory_gib, args.pod_cpu, args.pod_memory_gib
    )

    targets = [r for r in records["durations"] if r["status"] == args.status]
    if not targets:
        raise SystemExit(f"상태 {args.status}인 실험 기록이 없습니다.")
    minutes = _summarize([r["seconds"] / 60 for r in targets])

    usage_by_experiment = collect_usage(
        records["logs"],
        output_ratio=args.legacy_output_ratio,
        cache_ratio=args.legacy_cache_ratio,
    )
    target_ids = {r["id"] for r in targets}
    per_experiment = {
        experiment_id: stages
        for experiment_id, stages in usage_by_experiment.items()
        if experiment_id in target_ids
    }

    print(f"# 실험 원가 리포트 (status={args.status})\n")
    print(f"노드풀 machine type : {args.machine_type} ({vcpu} vCPU / {memory_gib:g} GiB)")
    print(
        f"단가 (실측, {args.region}) : vCPU ${price.vcpu_hour:.8f}/h, "
        f"RAM ${price.gib_hour:.8f}/GiB·h"
    )
    print(f"  → 노드 1대            : ${price.node_hour:.5f}/h")
    print(
        f"  → Pod request 1몫     : ${price.pod_hour:.5f}/h "
        f"({args.pod_cpu:g} vCPU / {args.pod_memory_gib:g} GiB)\n"
    )
    print(f"실험 수 : {minutes['n']}건")
    print(
        "벽시계  : 평균 {mean:.1f}분 / 중앙 {median:.1f}분 / "
        "최소 {min:.1f}분 / 최대 {max:.1f}분\n".format(**minutes)
    )

    _print_compute(args, price, minutes)

    if not per_experiment:
        print("## Codex 토큰 : 로그에서 사용량을 찾지 못했습니다.")
        return 0
    _print_tokens(
        args,
        TokenPrice(
            input_usd=args.token_price_input,
            cached_usd=args.token_price_cached,
            output_usd=args.token_price_output,
        ),
        per_experiment,
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
