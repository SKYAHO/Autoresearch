"""서빙에서 ONNX 모델을 LightGBM 호환 확률 모델처럼 쓰는 어댑터.

전체 파이프라인 기준 이 모듈이 담당하는 구간:
- **담당**: 학습이 기록한 ONNX 모델(`model_onnx/`)을 `onnxruntime`으로 추론해, 서빙의
  `ProbabilityModel` 계약(`predict_proba(DataFrame) -> (n, 2)`)을 그대로 만족시킨다. pandas
  category dtype 컬럼을 학습 시점 카테고리 순서의 **정수 코드**(`.cat.codes`)로 바꿔 단일
  float32 텐서로 넣는다 — 이렇게 하면 ONNX 예측이 원본 LightGBM과 허용오차 내로 일치한다(#302).
- **담당 아님(인접 책임)**: LightGBM→ONNX 변환은 학습측 `src.utils.model_utils.convert_lgbm_to_onnx`,
  아티팩트 로딩·joblib 폴백·페어링은 `src.serving.model_loader`, 재랭킹·calibration 체이닝은
  `src.serving.service.Reranker`가 담당한다. 이 어댑터는 predict_proba만 제공한다.

`Reranker`는 이 어댑터를 기존 joblib 모델과 동일한 `predict_proba` 인터페이스로 쓰므로,
main → calibration 체이닝 등 서빙 로직은 바뀌지 않는다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


class OnnxProbabilityModel:
    """`onnxruntime.InferenceSession`을 감싸 `predict_proba(DataFrame)`를 제공한다."""

    def __init__(self, session, feature_columns: tuple[str, ...]) -> None:
        self._session = session
        self._feature_columns = tuple(feature_columns)
        self._input_name = session.get_inputs()[0].name

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray:
        """학습 피처 순서로 float32 행렬을 만들어 ONNX 추론, `(n, 2)` 확률을 반환한다.

        category dtype 컬럼은 학습 시점 카테고리 순서의 정수 코드(`.cat.codes`)로, 나머지는
        float로 인코딩한다 — LightGBM이 내부적으로 쓰는 코드와 동일해 예측이 일치한다.
        """
        matrix = np.empty((len(features), len(self._feature_columns)), dtype=np.float32)
        for i, column in enumerate(self._feature_columns):
            series = features[column]
            if isinstance(series.dtype, pd.CategoricalDtype):
                matrix[:, i] = series.cat.codes.to_numpy(dtype=np.float32)
            else:
                matrix[:, i] = series.to_numpy(dtype=np.float32)

        outputs = self._session.run(None, {self._input_name: matrix})
        # zipmap=False로 변환했으므로 확률은 (n, 2) 텐서다(label 출력은 1D). 2차원 출력을 고른다.
        probabilities = next(
            (out for out in outputs if getattr(out, "ndim", 0) == 2), None
        )
        if probabilities is None:
            raise ValueError("ONNX 모델 출력에서 (n, 2) 확률 텐서를 찾지 못했습니다.")
        return np.asarray(probabilities, dtype=float)
