#!/usr/bin/env python3
"""
모델 훈련 스크립트.

config.yaml의 설정을 읽어 LightGBM 모델을 훈련하고 저장한다.
"""

import os
import yaml
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

from src.models.lgbm_model import LGBMModel
from src.utils.model_utils import save_model, save_feature_columns
from src.utils.config_utils import get_project_root, load_config


def main(config_path=None, data_path=None, model_output=None, test_size=None, random_state=None):
    project_root = get_project_root()
    if config_path is None:
        config_path = os.path.join(project_root, "src", "pipeline", "config.yaml")
    config = load_config(config_path)

    # CLI override 패턴: is not None으로 체크하여 falsy 값 처리
    if data_path is None:
        data_path = os.path.join(project_root, config["data"]["path"])
    if model_output is None:
        model_output = os.path.join(project_root, config["artifacts"]["model_path"])
    if test_size is None:
        test_size = config["data"]["test_size"]
    if random_state is None:
        random_state = config["data"]["random_state"]

    print("=" * 70)
    print("LightGBM 모델 훈련")
    print("=" * 70)

    # =========================================================
    # Step 1: 데이터 로드
    # =========================================================
    print("\n[Step 1] 데이터 로드...")
    dataset = pd.read_csv(data_path)
    print(f"  [OK] {len(dataset)} rows, {len(dataset.columns)} columns")

    # =========================================================
    # Step 2: Feature/Label 분리
    # =========================================================
    print("\n[Step 2] Feature/Label 분리...")
    feature_columns = config["data"]["feature_columns"]
    categorical_columns = config["data"]["categorical_columns"]

    X = dataset[feature_columns].copy()
    y = dataset["clicked"].copy()

    print(f"  [OK] Features: {X.shape}")
    print(f"  [OK] Label (clicked): {y.shape}, ratio={y.mean():.3%}")

    # =========================================================
    # Step 3: Categorical dtype 변환
    # =========================================================
    print("\n[Step 3] Categorical 컬럼 dtype 변환...")
    for col in categorical_columns:
        if col in X.columns:
            X[col] = X[col].astype("category")
    print(f"  [OK] {len(categorical_columns)} categorical columns 설정")

    # =========================================================
    # Step 4: Train/Val split
    # =========================================================
    print("\n[Step 4] Train/Val 분할...")
    X_train, X_val, y_train, y_val = train_test_split(
        X, y,
        test_size=test_size,
        random_state=random_state,
        stratify=y
    )
    print(f"  [OK] Train: {X_train.shape}, Val: {X_val.shape}")

    # =========================================================
    # Step 5: scale_pos_weight 계산
    # =========================================================
    print("\n[Step 5] scale_pos_weight 계산...")
    scale_pos_weight = config["model"]["scale_pos_weight"]
    if scale_pos_weight == "auto":
        neg_count = (y_train == 0).sum()
        pos_count = (y_train == 1).sum()
        scale_pos_weight = neg_count / pos_count
        print(f"  [OK] auto 계산: neg={neg_count}, pos={pos_count}, ratio={scale_pos_weight:.2f}")
    else:
        print(f"  [OK] 고정값: {scale_pos_weight}")

    # =========================================================
    # Step 6: 모델 훈련
    # =========================================================
    print("\n[Step 6] LightGBM 모델 훈련...")
    model = LGBMModel(
        scale_pos_weight=scale_pos_weight,
        n_estimators=config["model"]["n_estimators"],
        learning_rate=config["model"]["learning_rate"],
        num_leaves=config["model"]["num_leaves"],
        random_state=config["model"]["random_state"],
    )
    model.fit(X_train, y_train, categorical_features=categorical_columns)
    print(f"  [OK] 훈련 완료")

    # =========================================================
    # Step 7: 검증
    # =========================================================
    print("\n[Step 7] 검증...")
    y_val_pred_proba = model.predict_proba(X_val)[:, 1]
    val_roc_auc = roc_auc_score(y_val, y_val_pred_proba)
    print(f"  [OK] Val ROC-AUC: {val_roc_auc:.4f}")

    # category_match silent bug 감지
    if (X_val["category_match"] == 1).sum() == 0:
        print(f"  ⚠️  category_match에 1이 없음 (dtype 불일치 가능성)")

    # =========================================================
    # Step 8: 모델 저장
    # =========================================================
    print("\n[Step 8] 모델 저장...")
    feature_columns_path = os.path.join(project_root, config["artifacts"]["feature_columns_path"])

    save_model(model.model, model_output)
    save_feature_columns(feature_columns, feature_columns_path)

    print("\n" + "=" * 70)
    print("훈련 완료")
    print("=" * 70)
    print(f"Val ROC-AUC: {val_roc_auc:.4f}")
    print(f"Model: {model_output}")
    print(f"Feature columns: {feature_columns_path}")


if __name__ == "__main__":
    main()
