#!/usr/bin/env python3
"""
모델 훈련 스크립트.

config.yaml의 설정을 읽어 LightGBM 모델을 훈련하고 저장한다.
"""

import os
import sys
import yaml
import pandas as pd
from sklearn.model_selection import train_test_split
from sklearn.metrics import roc_auc_score

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.models.lgbm_model import LGBMModel  # noqa: E402
from src.utils.model_utils import save_model, save_feature_columns  # noqa: E402


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
    model_output: str = None,
    test_set_output: str = None,
    feature_columns_output: str = None,
    test_size: float = None,
    val_size: float = None,
    random_state: int = None,
):
    project_root = get_project_root()
    if config_path is None:
        config_path = os.path.join(project_root, "src", "pipeline", "config.yaml")
    elif not os.path.isabs(config_path):
        config_path = os.path.join(project_root, config_path)
    config = load_config(config_path)

    print("=" * 70)
    print("LightGBM 모델 훈련")
    print("=" * 70)

    print("\n[Step 1] 데이터 로드...")
    if data_path is None:
        data_path = os.path.join(project_root, config["data"]["path"])
    elif not os.path.isabs(data_path):
        data_path = os.path.join(project_root, data_path)
    dataset = pd.read_csv(data_path)
    print(f"  [OK] {len(dataset)} rows, {len(dataset.columns)} columns")

    print("\n[Step 2] Train/Val/Test 분할 (Test는 완전 held-out)...")
    if test_size is None:
        test_size = config["data"]["test_size"]
    if val_size is None:
        val_size = config["data"]["val_size"]
    if random_state is None:
        random_state = config["data"]["random_state"]

    if test_size + val_size >= 1:
        raise ValueError(
            f"test_size({test_size}) + val_size({val_size}) >= 1 입니다 — "
            "train에 데이터가 남지 않거나 분할 자체가 불가능합니다. "
            "두 값의 합이 1보다 작아야 합니다 (예: test_size=0.2, val_size=0.2)."
        )

    train_val_df, test_df = train_test_split(
        dataset,
        test_size=test_size,
        random_state=random_state,
        stratify=dataset["clicked"],
    )
    val_ratio_within_train_val = val_size / (1 - test_size)
    train_df, val_df = train_test_split(
        train_val_df,
        test_size=val_ratio_within_train_val,
        random_state=random_state,
        stratify=train_val_df["clicked"],
    )
    print(f"  [OK] Train: {len(train_df)}, Val: {len(val_df)}, Test: {len(test_df)} (Test는 학습에 미사용)")

    if test_set_output is None:
        test_set_path = os.path.join(project_root, config["artifacts"]["test_set_path"])
    elif not os.path.isabs(test_set_output):
        test_set_path = os.path.join(project_root, test_set_output)
    else:
        test_set_path = test_set_output
    os.makedirs(os.path.dirname(test_set_path), exist_ok=True)
    test_df.to_csv(test_set_path, index=False)
    print(f"  [저장] Test set (held-out): {test_set_path}")

    print("\n[Step 3] Feature/Label 분리...")
    feature_columns = config["data"]["feature_columns"]
    categorical_columns = config["data"]["categorical_columns"]

    X_train = train_df[feature_columns].copy()
    y_train = train_df["clicked"].copy()
    X_val = val_df[feature_columns].copy()
    y_val = val_df["clicked"].copy()

    print(f"  [OK] Train features: {X_train.shape}, ratio={y_train.mean():.3%}")
    print(f"  [OK] Val features: {X_val.shape}, ratio={y_val.mean():.3%}")

    print("\n[Step 4] Categorical 컬럼 dtype 변환...")
    for col in categorical_columns:
        if col in X_train.columns:
            categories = pd.api.types.union_categoricals(
                [X_train[col].astype("category"), X_val[col].astype("category")]
            ).categories
            X_train[col] = pd.Categorical(X_train[col], categories=categories)
            X_val[col] = pd.Categorical(X_val[col], categories=categories)
    print(f"  [OK] {len(categorical_columns)} categorical columns 설정")

    print("\n[Step 5] scale_pos_weight 계산...")
    scale_pos_weight = config["model"]["scale_pos_weight"]
    if scale_pos_weight == "auto":
        neg_count = (y_train == 0).sum()
        pos_count = (y_train == 1).sum()
        scale_pos_weight = neg_count / pos_count
        print(f"  [OK] auto 계산: neg={neg_count}, pos={pos_count}, ratio={scale_pos_weight:.2f}")
    else:
        print(f"  [OK] 고정값: {scale_pos_weight}")

    print("\n[Step 6] LightGBM 모델 훈련...")
    model = LGBMModel(
        scale_pos_weight=scale_pos_weight,
        n_estimators=config["model"]["n_estimators"],
        learning_rate=config["model"]["learning_rate"],
        num_leaves=config["model"]["num_leaves"],
        random_state=random_state,
    )
    model.fit(X_train, y_train, categorical_features=categorical_columns)
    print("  [OK] 훈련 완료")

    print("\n[Step 7] 검증...")
    y_val_pred_proba = model.predict_proba(X_val)[:, 1]
    val_roc_auc = roc_auc_score(y_val, y_val_pred_proba)
    print(f"  [OK] Val ROC-AUC: {val_roc_auc:.4f}")

    if (X_val["historical_category_match"] == 1).sum() == 0:
        print("  ⚠️  historical_category_match에 1이 없음 (dtype 불일치 가능성)")

    print("\n[Step 8] 모델 저장...")
    if model_output is None:
        model_path = os.path.join(project_root, config["artifacts"]["model_path"])
    elif not os.path.isabs(model_output):
        model_path = os.path.join(project_root, model_output)
    else:
        model_path = model_output
    if feature_columns_output is None:
        feature_columns_path = os.path.join(project_root, config["artifacts"]["feature_columns_path"])
    elif not os.path.isabs(feature_columns_output):
        feature_columns_path = os.path.join(project_root, feature_columns_output)
    else:
        feature_columns_path = feature_columns_output

    save_model(model.model, model_path)
    save_feature_columns(feature_columns, feature_columns_path)

    print("\n" + "=" * 70)
    print("훈련 완료")
    print("=" * 70)
    print(f"Val ROC-AUC: {val_roc_auc:.4f}")
    print(f"Model: {model_path}")
    print(f"Feature columns: {feature_columns_path}")


if __name__ == "__main__":
    main()
