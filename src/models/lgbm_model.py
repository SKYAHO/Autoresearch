"""LightGBM 모델 wrapper.

[파이프라인] 학습(src/pipeline/train.py)이 사용하는 champion 계열 모델 구현체.
model_contract 스칼라 피처를 축정렬 분할(tree split)로 학습한다.

[기능] src.models.base.CTRModel 인터페이스(fit/predict_proba/save/load)를 구현해,
향후 train.py가 다른 모델 구현체와 다형적으로 다룰 수 있게 한다. save/load는 기존
프로덕션 경로(src.utils.model_utils.save_model/load_model이 raw LightGBM booster를
직접 joblib 저장)와 동일한 결과를 내도록 override한다 — 인터페이스 추가가 기존 저장
아티팩트 포맷을 바꾸지 않는다(additive,
docs/archive/specs/2026-07-30-ctr-model-interface-port.md 참고).

[비책임] ONNX 변환은 여전히 src.utils.model_utils.convert_lgbm_to_onnx가 전담한다
(이 클래스는 변환하지 않는다).
"""

import lightgbm as lgb
import numpy as np
import pandas as pd

from src.models.base import CTRModel
from src.utils.model_utils import load_model, save_model


class LGBMModel(CTRModel):
    """LightGBM 이진 분류 모델 wrapper."""

    def __init__(
        self,
        scale_pos_weight: float,
        n_estimators: int = 200,
        learning_rate: float = 0.05,
        num_leaves: int = 63,
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

    def save(self, path: str) -> None:
        """raw LightGBM booster(self.model)를 joblib으로 저장한다.

        기존 src.pipeline.train이 호출해온 save_model(model.model, model_path)와
        동일한 결과물(raw booster, wrapper 아님)을 만든다 — 서빙(model_loader)이
        이 포맷을 그대로 기대하므로 아티팩트 포맷은 바뀌지 않는다.
        """
        if self.model is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        save_model(self.model, path)

    @classmethod
    def load(cls, path: str) -> "LGBMModel":
        """joblib으로 저장된 raw LightGBM booster를 불러와 wrapper에 담는다.

        복원한 booster의 get_params()로 wrapper의 하이퍼파라미터 속성도 함께
        복원한다 — 그렇지 않으면 load() 이후 wrapper에 다시 fit()을 호출했을 때
        저장 당시가 아니라 생성자 기본값으로 학습되는 오류가 생긴다.
        """
        booster = load_model(path)
        params = booster.get_params()
        instance = cls(
            scale_pos_weight=params.get("scale_pos_weight", 1),
            n_estimators=params.get("n_estimators", 200),
            learning_rate=params.get("learning_rate", 0.05),
            num_leaves=params.get("num_leaves", 63),
            random_state=params.get("random_state", 42),
        )
        instance.model = booster
        return instance
