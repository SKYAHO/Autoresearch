#!/usr/bin/env python3
"""학습 윈도우 arm을 **공통 홀드아웃**으로 채점한다.

[파이프라인] 학습/평가 구간의 **실험 보조** 도구다. 모델을 등록하지 않고(defer_registration),
지표만 낸다.

[왜 필요한가] `sweep-seeds`가 내는 `val_roc_auc`는 각 arm이 **자기 데이터에서 뽑은** val로
잰 값이다. 윈도우가 다르면 val의 구성도 달라지므로, arm 간 비교에서 "데이터가 늘어 좋아진
것"과 "val 문제가 쉬워진 것"이 섞인다. 실제로 2026-07-31 1차 측정에서 옛날 하루를 추가하자
val AUC가 +0.032 뛰었는데, 데이터량 효과로 보기엔 너무 컸다.

이 스크립트는 **모든 arm을 동일한 홀드아웃 CSV로** 채점해 그 교란을 없앤다. 홀드아웃은
어떤 arm도 학습에 쓰지 않는 미래 구간이어야 한다 — 프로덕션이 하는 일(과거로 학습해
다음 날을 예측)과 같은 형태다.

출력은 `sweep-seeds --result-path`와 같은 스키마라 `compare_seed_sweeps.py`로 바로 비교된다.
"""

import argparse
import json
import os
import sys

import pandas as pd
from sklearn.metrics import roc_auc_score

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.features.model_contract import CATEGORICAL_FEATURE_COLUMNS  # noqa: E402
from src.pipeline import train  # noqa: E402
from src.pipeline.seed_sweep import summarize_metric, validate_seeds  # noqa: E402
from src.utils.model_utils import load_feature_columns, load_model  # noqa: E402


def score_holdout(model_path: str, feature_columns_path: str, holdout: pd.DataFrame) -> float:
    """학습된 모델을 홀드아웃에서 채점한다.

    ROC-AUC는 순위 기반이라 downsampling 보정에 불변이므로(#300 결정 5) 보정 없이 잰다.
    """
    model = load_model(model_path)
    feature_columns = list(load_feature_columns(feature_columns_path))
    X = holdout[feature_columns].copy()
    for column in CATEGORICAL_FEATURE_COLUMNS:
        if column in X.columns:
            X[column] = X[column].astype("category")
    return float(roc_auc_score(holdout["clicked"], model.predict_proba(X)[:, 1]))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--arm-csv", required=True, help="이 arm의 학습 데이터 CSV")
    parser.add_argument("--holdout-csv", required=True, help="모든 arm이 공유하는 홀드아웃 CSV")
    parser.add_argument("--seeds", default="42,43,44,45,46", help="쉼표 구분 시드 목록")
    parser.add_argument("--output-dir", required=True, help="시드별 아티팩트 저장 디렉토리")
    parser.add_argument("--result-path", required=True, help="요약 JSON 저장 경로")
    parser.add_argument("--experiment", default=None, help="MLflow 실험 이름(prod와 분리)")
    args = parser.parse_args()

    seeds = [int(s.strip()) for s in args.seeds.split(",") if s.strip()]
    validate_seeds(seeds)
    os.makedirs(args.output_dir, exist_ok=True)

    holdout = pd.read_csv(args.holdout_csv)
    print(f"[홀드아웃] {args.holdout_csv}: {len(holdout):,}행, "
          f"클릭 {int(holdout['clicked'].sum()):,} ({holdout['clicked'].mean():.4f})")

    metrics = []
    for seed in seeds:
        model_path = os.path.join(args.output_dir, f"model_seed{seed}.joblib")
        columns_path = os.path.join(args.output_dir, f"feature_columns_seed{seed}.json")
        print(f"\n[시드 {seed}] 학습...")
        train.main(
            # 스윕 산출물은 승격 후보가 아니다 — 등록 없이 지표만 받는다.
            defer_registration=True,
            data_path=args.arm_csv,
            model_output=model_path,
            # test_set은 arm마다 달라 비교에 쓰지 않지만, 시드끼리 덮어쓰지 않게 분리한다.
            test_set_output=os.path.join(args.output_dir, f"test_set_seed{seed}.csv"),
            feature_columns_output=columns_path,
            categorical_columns_output=os.path.join(
                args.output_dir, f"categorical_seed{seed}.json"
            ),
            random_state=seed,
            experiment=args.experiment,
        )
        score = score_holdout(model_path, columns_path, holdout)
        print(f"[시드 {seed}] 홀드아웃 ROC-AUC = {score:.4f}")
        metrics.append(score)

    summary = summarize_metric(metrics)
    result = {
        "metric_name": "holdout_roc_auc",
        "holdout_path": args.holdout_csv,
        "holdout_rows": int(len(holdout)),
        "arm_csv": args.arm_csv,
        "seeds": seeds,
        "metrics": metrics,
        "summary": summary.to_dict(),
    }
    os.makedirs(os.path.dirname(os.path.abspath(args.result_path)) or ".", exist_ok=True)
    with open(args.result_path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"\n[저장] {args.result_path}")
    print(f"  mean={summary.mean:.4f} std={summary.std:.4f} "
          f"min={summary.minimum:.4f} max={summary.maximum:.4f}")


if __name__ == "__main__":
    main()
