#!/usr/bin/env python3
"""
모델 평가 스크립트.

저장된 모델을 로드하여 ROC-AUC, PR-AUC, Log Loss를 계산하고,
baseline 모델과 비교한다.
"""

import os
import sys
import yaml
import pickle
import pandas as pd
from sklearn.metrics import (  # noqa: E402
    roc_auc_score,
    average_precision_score,
    log_loss,
    brier_score_loss,
)

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.utils.model_utils import load_model, load_feature_columns  # noqa: E402
from src.models.downsampling import apply_downsampling_calibration  # noqa: E402
from src.features.model_contract import (  # noqa: E402
    CATEGORICAL_FEATURE_COLUMNS,
    require_model_feature_columns,
)


def get_project_root():
    """프로젝트 루트 경로 반환."""
    current = os.path.dirname(os.path.abspath(__file__))
    while current != "/":
        if os.path.exists(os.path.join(current, "src")):
            return current
        current = os.path.dirname(current)
    raise RuntimeError("프로젝트 루트를 찾을 수 없습니다")


def load_config(config_path):
    """config.yaml 로드."""
    with open(config_path, "r") as f:
        return yaml.safe_load(f)


def main(
    config_path: str = None,
    data_path: str = None,
    model_path: str = None,
    feature_columns_path: str = None,
    sampling_rate: float = 1.0,
):
    # sampling_rate: 학습 시 쓴 negative downsampling 실현 비율(#300). 다운샘플된
    # 분포로 학습된 모델의 출력 확률을 원분포로 보정해 LogLoss/Brier/calibration을
    # 올바른 분포에서 잰다. 기본 1.0 = 보정 없음(항등) — downsampling 미사용
    # 모델이나 standalone 평가의 하위호환 기본값(#300 결정 7).
    project_root = get_project_root()
    if config_path is None:
        config_path = os.path.join(project_root, "src", "pipeline", "config.yaml")
    elif not os.path.isabs(config_path):
        config_path = os.path.join(project_root, config_path)
    config = load_config(config_path)

    print("=" * 70)
    print("모델 평가")
    print("=" * 70)

    print("\n[Step 1] 모델 로드...")
    if model_path is None:
        model_path = os.path.join(project_root, config["artifacts"]["model_path"])
    elif not os.path.isabs(model_path):
        model_path = os.path.join(project_root, model_path)
    if feature_columns_path is None:
        feature_columns_path = os.path.join(project_root, config["artifacts"]["feature_columns_path"])
    elif not os.path.isabs(feature_columns_path):
        feature_columns_path = os.path.join(project_root, feature_columns_path)

    model = load_model(model_path)
    feature_columns = require_model_feature_columns(load_feature_columns(feature_columns_path))

    print("\n[Step 2] 데이터 로드 (held-out test set)...")
    if data_path is None:
        data_path = os.path.join(project_root, config["artifacts"]["test_set_path"])
    elif not os.path.isabs(data_path):
        data_path = os.path.join(project_root, data_path)
    dataset = pd.read_csv(data_path)

    X = dataset[list(feature_columns)].copy()
    y = dataset["clicked"].copy()

    for column in CATEGORICAL_FEATURE_COLUMNS:
        X[column] = X[column].astype("category")

    print(f"  [OK] {len(dataset)} rows")

    print("\n[Step 3] 예측...")
    raw_pred_proba = model.predict_proba(X)[:, 1]
    # downsampling 보정(#300 결정 4). sampling_rate=1.0이면 항등(no-op).
    # 보정은 monotonic이라 ROC-AUC/PR-AUC/랭킹 지표는 불변이고, LogLoss/Brier/
    # calibration만 원분포 기준으로 이동한다(결정 5).
    y_pred_proba = apply_downsampling_calibration(raw_pred_proba, sampling_rate)
    if sampling_rate < 1.0:
        print(f"  [OK] 예측 완료 (downsampling 보정 적용, sampling_rate={sampling_rate})")
    else:
        print("  [OK] 예측 완료 (보정 없음)")

    print("\n[Step 4] 평가 지표 계산...")
    # AUC 계열은 순위 기반이라 보정 전/후 동일하므로 어느 확률로 재도 같다.
    roc_auc = roc_auc_score(y, y_pred_proba)
    pr_auc = average_precision_score(y, y_pred_proba)
    # LogLoss/Brier는 보정된 확률(원분포 기준)로 잰다 — 보정 검증 근거(결정 5).
    logloss = log_loss(y, y_pred_proba)
    brier = brier_score_loss(y, y_pred_proba)

    print(f"  [OK] ROC-AUC: {roc_auc:.4f}  (보정에 불변)")
    print(f"  [OK] PR-AUC: {pr_auc:.4f}  (보정에 불변)")
    print(f"  [OK] Log Loss: {logloss:.4f}")
    print(f"  [OK] Brier: {brier:.4f}")
    # calibration 요약: 예측 평균 vs 실제 양성률(원분포 보정 후 서로 가까워야 함).
    print(
        f"  [OK] calibration: 예측 평균={float(y_pred_proba.mean()):.4f} "
        f"vs 실제 양성률={float(y.mean()):.4f}"
    )

    print("\n[Step 5] Baseline (LogisticRegression) 비교...")
    baseline_path = os.path.join(project_root, "models", "baseline.pkl")
    if os.path.exists(baseline_path):
        try:
            with open(baseline_path, "rb") as f:
                baseline_model = pickle.load(f)
            baseline_pred_proba = baseline_model.predict_proba(X)[:, 1]
            baseline_roc_auc = roc_auc_score(y, baseline_pred_proba)
            print(f"  [OK] Baseline ROC-AUC: {baseline_roc_auc:.4f}")
            print(f"  [OK] LightGBM vs Baseline: {roc_auc - baseline_roc_auc:+.4f}")
        except Exception as e:
            print(f"  [WARNING] Baseline 로드 실패: {e}")
    else:
        print(f"  [WARNING] Baseline 모델을 찾을 수 없음: {baseline_path}")

    print("\n" + "=" * 70)
    print("평가 완료")
    print("=" * 70)
    print(f"ROC-AUC: {roc_auc:.4f}")
    print(f"PR-AUC: {pr_auc:.4f}")
    print(f"Log Loss: {logloss:.4f}")


if __name__ == "__main__":
    main()
