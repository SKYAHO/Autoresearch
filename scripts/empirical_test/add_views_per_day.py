#!/usr/bin/env python3
"""#396 실험용 파생 피처(views_per_day) 주입 스크립트 (Auto Research 최소 흐름 B2).

[파이프라인] 피처 구간의 **실험 전용 우회로** — 정규 조립 경로가 아니다. 정규 학습 조립은
``src/pipeline/build_training_dataset.py``(Feast offline PIT, feast-only)가 소유하며, 그
경로는 BigQuery spine + GCS 레지스트리가 필요해 로컬에서 실행할 수 없다. 이 스크립트는
이미 조립된 학습 CSV를 읽어 기존 컬럼만으로 계산되는 파생 피처 1개를 덧붙여 새 CSV로 쓴다.

이 모듈이 담당하지 않는 것: 피처 정의의 정본화(Feast FeatureView/ODFV), 모델 입력 계약
(``src/features/model_contract.py``), 학습·평가(``src/pipeline/train.py`` / ``evaluate.py``).
파생 피처를 실제 파이프라인에 반영하려면 Feast ODFV로 정의하고 apply해야 하며, 이 스크립트는
**그 전에 가설의 값어치만 오프라인으로 재보는 용도**다.

기능:
  - views_per_day = view_count / (days_since_upload + 1) 계산 후 컬럼 추가
  - 입력 CSV의 나머지 컬럼·행 순서는 그대로 보존(라벨 clicked 포함)
"""

from __future__ import annotations

import argparse
import os
import sys

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

DERIVED_COLUMN = "views_per_day"


def add_views_per_day(dataset: pd.DataFrame) -> pd.DataFrame:
    """view_count / (days_since_upload + 1)을 계산해 컬럼으로 붙인다.

    +1은 업로드 당일(days_since_upload=0, 표본의 26%)에서 0으로 나누지 않기 위한 것이다.
    cold-start로 0이 채워진 영상 미발견 행(표본의 약 10%)은 분자도 0이라 결과도 0이 된다 —
    "속도 없음"으로 남고 별도 결측 표시를 만들지 않는다(계약에 결측 개념이 없다).
    """
    missing = [c for c in ("view_count", "days_since_upload") if c not in dataset.columns]
    if missing:
        raise ValueError(f"입력 CSV에 필요한 컬럼이 없습니다: {missing}")
    result = dataset.copy()
    result[DERIVED_COLUMN] = result["view_count"] / (result["days_since_upload"] + 1)
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", required=True, help="입력 학습 CSV 경로")
    parser.add_argument("--output", required=True, help="파생 피처를 붙인 출력 CSV 경로")
    args = parser.parse_args()

    print(f"[로드] {args.input}")
    dataset = pd.read_csv(args.input)
    print(f"  [OK] {len(dataset)} rows, {len(dataset.columns)} columns")

    dataset = add_views_per_day(dataset)
    stats = dataset[DERIVED_COLUMN]
    print(
        f"[파생] {DERIVED_COLUMN}: median={stats.median():.1f} "
        f"mean={stats.mean():.1f} zero%={float((stats == 0).mean()) * 100:.2f}"
    )

    os.makedirs(os.path.dirname(os.path.abspath(args.output)), exist_ok=True)
    dataset.to_csv(args.output, index=False)
    print(f"[저장] {args.output} ({len(dataset)} rows, {len(dataset.columns)} columns)")


if __name__ == "__main__":
    main()
