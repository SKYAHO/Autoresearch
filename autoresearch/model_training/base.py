"""CTR 이진 분류 모델 계열의 최소 추상 인터페이스.

[파이프라인] 학습(autoresearch/model_training/train.py)이 모델 구현체(LGBMModel 등)를 다형적으로
다루기 위한 계약만 이 모듈이 정의한다. 각 구현체의 알고리즘·하이퍼파라미터·전처리는
개별 모듈(lgbm_model.py 등)이 소유한다.

[기능] fit/predict_proba를 각 구현체가 반드시 구현하도록 강제하고, save/load는
joblib 직렬화 기본 구현을 제공해 구현체가 필요할 때만 override하게 한다.

[비책임] 모델 서빙(applications/reranking_api/*), ONNX 변환(autoresearch.model_training.model_utils
.convert_lgbm_to_onnx), MLflow 아티팩트 로깅(autoresearch/model_training/train.py)은 이 인터페이스를
소비하지 않는다 — 기존 LightGBM 프로덕션 저장 경로는 이 인터페이스 도입으로 바뀌지
않는다(additive, capability probe round_001/round_002 검증을 거쳐 포팅,
docs/archive/specs/2026-07-30-ctr-model-interface-port.md 참고).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import Optional

import numpy as np
import pandas as pd


class CTRModel(ABC):
    """CTR 이진 분류 모델이 공통으로 따르는 최소 인터페이스."""

    @abstractmethod
    def fit(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        categorical_features: Optional[list] = None,
    ) -> None:
        """모델을 학습한다."""
        raise NotImplementedError

    @abstractmethod
    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """클릭 확률을 예측한다.

        Returns:
            (n_samples, 2) shape. 각 행: [P(click=0), P(click=1)]
        """
        raise NotImplementedError

    def save(self, path: str) -> None:
        """joblib으로 self 전체를 직렬화하는 기본 구현.

        구현체가 다른 저장 방식(예: 프레임워크 네이티브 객체만 저장)이 필요하면
        override한다(LGBMModel이 그 예).
        """
        import joblib

        parent = os.path.dirname(path)
        if parent:
            os.makedirs(parent, exist_ok=True)
        joblib.dump(self, path)

    @classmethod
    def load(cls, path: str) -> "CTRModel":
        """joblib으로 역직렬화하는 기본 구현.

        역직렬화된 객체가 cls의 인스턴스가 아니면 TypeError를 던진다 — 구현체가
        save()를 override해 다른 타입(예: 프레임워크 네이티브 객체)을 저장했다면
        load()도 함께 override해야 한다는 계약을 지키지 않은 경우를 조용히
        통과시키지 않기 위함이다.
        """
        import joblib

        if not os.path.exists(path):
            raise FileNotFoundError(f"모델 파일을 찾을 수 없습니다: {path}")
        obj = joblib.load(path)
        if not isinstance(obj, cls):
            raise TypeError(
                f"{path}에서 역직렬화한 객체가 {cls.__name__} 인스턴스가 아닙니다: "
                f"{type(obj).__name__}. save()를 override했다면 load()도 함께 "
                "override해야 합니다."
            )
        return obj
