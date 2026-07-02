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
from sklearn.metrics import roc_auc_score, average_precision_score, log_loss

# Add project root to path
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.utils.model_utils import load_model, load_feature_columns


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


def main():
    project_root = get_project_root()
    config_path = os.path.join(project_root, "src", "pipeline", "config.yaml")
    config = load_config(config_path)

    print("=" * 70)
    print("모델 평가")
    print("=" * 70)

    # =========================================================
    # Step 1: 모델 & Feature 로드
    # =========================================================
    print("\n[Step 1] 모델 로드...")
    model_path = os.path.join(project_root, config["artifacts"]["model_path"])
    feature_columns_path = os.path.join(project_root, config["artifacts"]["feature_columns_path"])

    model = load_model(model_path)
    feature_columns = load_feature_columns(feature_columns_path)

    # =========================================================
    # Step 2: 데이터 로드 및 전처리
    # =========================================================
    print("\n[Step 2] 데이터 로드...")
    data_path = os.path.join(project_root, config["data"]["path"])
    dataset = pd.read_csv(data_path)

    X = dataset[feature_columns].copy()
    y = dataset["clicked"].copy()

    # Categorical 변환
    categorical_columns = config["data"]["categorical_columns"]
    for col in categorical_columns:
        if col in X.columns:
            X[col] = X[col].astype("category")

    print(f"  [OK] {len(dataset)} rows")

    # =========================================================
    # Step 3: 예측
    # =========================================================
    print("\n[Step 3] 예측...")
    y_pred_proba = model.predict_proba(X)[:, 1]
    print(f"  [OK] 예측 완료")

    # =========================================================
    # Step 4: 평가 지표 계산
    # =========================================================
    print("\n[Step 4] 평가 지표 계산...")
    roc_auc = roc_auc_score(y, y_pred_proba)
    pr_auc = average_precision_score(y, y_pred_proba)
    logloss = log_loss(y, y_pred_proba)

    print(f"  [OK] ROC-AUC: {roc_auc:.4f}")
    print(f"  [OK] PR-AUC: {pr_auc:.4f}")
    print(f"  [OK] Log Loss: {logloss:.4f}")

    # =========================================================
    # Step 5: Baseline 비교
    # =========================================================
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
