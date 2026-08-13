"""서빙에서 ONNX 모델을 LightGBM 호환 확률 모델처럼 쓰는 어댑터.

전체 파이프라인 기준 이 모듈이 담당하는 구간:
- **담당**: 학습이 기록한 ONNX 모델(`model_onnx/`)을 `onnxruntime`으로 추론해, 서빙의
  `ProbabilityModel` 계약(`predict_proba(DataFrame) -> (n, 2)`)을 그대로 만족시킨다. pandas
  category dtype 컬럼을 학습 시점 카테고리 순서의 **정수 코드**(`.cat.codes`)로 바꿔 단일
  float32 텐서로 넣는다 — 이렇게 하면 ONNX 예측이 원본 LightGBM과 허용오차 내로 일치한다(#302).
- **담당 아님(인접 책임)**: LightGBM→ONNX 변환은 학습측 `autoresearch.model_training.model_utils.convert_lgbm_to_onnx`,
  아티팩트 manifest 검증·로딩은 `src.serving.model_loader`, 재랭킹·calibration 체이닝은
  `src.serving.service.Reranker`가 담당한다. 이 어댑터는 predict_proba만 제공한다.

`Reranker`는 이 어댑터를 기존 joblib 모델과 동일한 `predict_proba` 인터페이스로 쓰므로,
main → calibration 체이닝 등 서빙 로직은 바뀌지 않는다.
"""

from __future__ import annotations

import numpy as np
import pandas as pd


def validate_onnx_session_contract(session, feature_count: int) -> None:
    """ONNX 세션의 입력·확률 출력 계약을 기동 시 fail-closed 검증한다."""
    inputs = session.get_inputs()
    if len(inputs) != 1:
        raise ValueError("ONNX 입력은 정확히 하나여야 합니다.")
    input_metadata = inputs[0]
    if input_metadata.name != "input":
        raise ValueError("ONNX 입력 이름은 input이어야 합니다.")
    if input_metadata.type != "tensor(float)":
        raise ValueError("ONNX 입력 dtype은 tensor(float)이어야 합니다.")
    input_shape = list(input_metadata.shape)
    if len(input_shape) != 2 or input_shape[1] != feature_count:
        raise ValueError(
            f"ONNX 입력 shape는 [batch, {feature_count}]여야 합니다: {input_shape}"
        )
    if input_shape[0] is not None and not isinstance(input_shape[0], str):
        raise ValueError(f"ONNX 입력 batch 차원은 동적이어야 합니다: {input_shape}")
    probability_outputs = [
        output
        for output in session.get_outputs()
        if output.type == "tensor(float)"
        and len(output.shape) == 2
        and output.shape[1] == 2
    ]
    if len(probability_outputs) != 1:
        raise ValueError("ONNX 출력에는 [batch, 2] float 확률 tensor가 정확히 하나여야 합니다.")
    output_shape = list(probability_outputs[0].shape)
    if output_shape[0] is not None and not isinstance(output_shape[0], str):
        raise ValueError(f"ONNX 출력 batch 차원은 동적이어야 합니다: {output_shape}")


class OnnxProbabilityModel:
    """`onnxruntime.InferenceSession`을 감싸 `predict_proba(DataFrame)`를 제공한다."""

    def __init__(
        self,
        session,
        feature_columns: tuple[str, ...],
        workspace_owner: object | None = None,
    ) -> None:
        validate_onnx_session_contract(session, len(feature_columns))
        self._session = session
        self._feature_columns = tuple(feature_columns)
        self._input_name = session.get_inputs()[0].name
        self._probability_output_name = next(
            output.name
            for output in session.get_outputs()
            if output.type == "tensor(float)"
            and len(output.shape) == 2
            and output.shape[1] == 2
        )
        # MLflow 다운로드 복사본을 소유한 TemporaryDirectory다. ORT 세션과 같은 모델
        # 인스턴스가 강하게 참조해 세션 수명보다 먼저 정리되는 TOCTOU를 막는다.
        self._workspace_owner = workspace_owner

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

        outputs = self._session.run(
            [self._probability_output_name], {self._input_name: matrix}
        )
        # zipmap=False로 변환했으므로 확률은 (n, 2) 텐서다(label 출력은 1D). 2차원 출력을 고른다.
        probabilities = next(
            (out for out in outputs if getattr(out, "ndim", 0) == 2), None
        )
        if probabilities is None:
            raise ValueError("ONNX 모델 출력에서 (n, 2) 확률 텐서를 찾지 못했습니다.")
        if probabilities.shape != (len(features), 2):
            raise ValueError(
                f"ONNX 확률 출력 shape가 예상과 다릅니다: {probabilities.shape}"
            )
        return np.asarray(probabilities, dtype=float)
