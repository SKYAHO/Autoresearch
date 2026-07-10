"""LightGBM 모델 wrapper."""

import lightgbm as lgb
import numpy as np
import pandas as pd


class LGBMModel:
    """LightGBM 이진 분류 모델 wrapper."""

    def __init__(
        self,
        scale_pos_weight: float,
        n_estimators: int = 200,
        learning_rate: float = 0.05,
        num_leaves: int = 31,
        random_state: int = 42,
    ):
        """
        초기화.

        Args:
            scale_pos_weight: 클래스 불균형 대응. neg_count / pos_count.
            n_estimators: 트리 개수.
            learning_rate: 학습률.
            num_leaves: 트리당 최대 리프 개수.
            random_state: 시드.
        """
        self.scale_pos_weight = scale_pos_weight
        self.n_estimators = n_estimators
        self.learning_rate = learning_rate
        self.num_leaves = num_leaves
        self.random_state = random_state
        self.model = None

    def fit(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        categorical_features: list = None,
    ) -> None:
        """
        모델 학습.

        Args:
            X_train: 훈련 feature.
            y_train: 훈련 label (0 또는 1).
            categorical_features: 카테고리 컬럼 이름 리스트.
        """
        if categorical_features is None:
            categorical_features = []

        self.model = lgb.LGBMClassifier(
            n_estimators=self.n_estimators,
            learning_rate=self.learning_rate,
            num_leaves=self.num_leaves,
            scale_pos_weight=self.scale_pos_weight,
            random_state=self.random_state,
            objective="binary",
            metric="auc",
            verbose=-1,
        )

        self.model.fit(
            X_train,
            y_train,
            categorical_feature=categorical_features,
        )

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """
        클릭 확률 예측.

        Returns:
            (n_samples, 2) shape. 각 행: [P(click=0), P(click=1)]
        """
        if self.model is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        return self.model.predict_proba(X)

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """
        클릭 여부 예측 (0 또는 1).

        Returns:
            (n_samples,) shape. 각 요소: 0 또는 1.
        """
        if self.model is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        return self.model.predict(X)
