#!/usr/bin/env python3
"""여러 `sweep-seeds` 결과 JSON을 baseline 대비로 비교한다.

[파이프라인] 학습/평가 구간의 **분석 보조** 도구다. 학습도 등록도 하지 않고, 이미 나온
시드 스윕 요약(`--result-path` 산출물)만 읽어 판정 근거를 만든다.

[기능] baseline 1개와 candidate N개를 받아 `autoresearch.model_evaluation.seed_sweep`의
`summarize_metric`/`compare_to_baseline`으로 유의성 근거를 낸다 — 판정 로직을 여기서
다시 구현하지 않고 정본을 그대로 쓴다.

**짝지음(paired)은 기본이 아니다.** `compare_to_baseline`의 짝지음 전제는 "같은 시드가
같은 분할을 본다"인데, 그건 **데이터셋이 동일할 때만** 성립한다. 학습 윈도우·데이터 소스가
다른 arm끼리는 같은 시드라도 다른 데이터를 보므로 독립 표본으로 비교한다.
데이터셋이 동일하고 피처·하이퍼파라미터만 다른 비교라면 `--paired`를 준다.

사용:
    python scripts/bench/compare_seed_sweeps.py \\
        --baseline result_A.json --candidate result_B.json --candidate result_C.json
"""

import argparse
import json
import os
import sys

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from autoresearch.model_evaluation.seed_sweep import compare_to_baseline, summarize_metric  # noqa: E402


def _load(path: str) -> tuple[str, list[int], list[float]]:
    with open(path, "r", encoding="utf-8") as f:
        data = json.load(f)
    return os.path.basename(path), data["seeds"], data["metrics"]


def _paired_deltas(
    base_seeds: list[int], base: list[float], cand_seeds: list[int], cand: list[float]
) -> list[float]:
    """같은 시드끼리 뺀 차이. 시드 목록이 다르면 짝지을 수 없으므로 거부한다."""
    if base_seeds != cand_seeds:
        raise ValueError(
            f"짝지으려면 시드 목록이 같아야 합니다: baseline={base_seeds} candidate={cand_seeds}"
        )
    return [c - b for c, b in zip(cand, base)]


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--baseline", required=True, help="기준 result JSON 경로")
    parser.add_argument(
        "--candidate", action="append", required=True, help="비교 대상 result JSON (반복 가능)"
    )
    parser.add_argument(
        "--paired",
        action="store_true",
        help="같은 데이터셋에 시드만 공유하는 비교일 때만 지정한다(윈도우·소스가 다르면 금지).",
    )
    args = parser.parse_args()

    base_name, base_seeds, base_metrics = _load(args.baseline)
    base_summary = summarize_metric(base_metrics)
    print(f"[baseline] {base_name}")
    print(
        f"  n={base_summary.n} mean={base_summary.mean:.4f} std={base_summary.std:.4f} "
        f"min={base_summary.minimum:.4f} max={base_summary.maximum:.4f}"
    )

    for path in args.candidate:
        name, seeds, metrics = _load(path)
        summary = summarize_metric(metrics)
        deltas = (
            _paired_deltas(base_seeds, base_metrics, seeds, metrics) if args.paired else None
        )
        verdict = compare_to_baseline(
            candidate=summary, baseline=base_summary, paired_deltas=deltas
        )
        print(f"\n[candidate] {name}{'  (paired)' if args.paired else '  (unpaired)'}")
        print(
            f"  n={summary.n} mean={summary.mean:.4f} std={summary.std:.4f} "
            f"min={summary.minimum:.4f} max={summary.maximum:.4f}"
        )
        for field, value in verdict.to_dict().items():
            print(f"  {field}: {value}")


if __name__ == "__main__":
    main()
