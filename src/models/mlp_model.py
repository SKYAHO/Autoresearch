"""NumPy 기반 2-layer CTR 모델.

이 구현은 학습 프레임워크에 의존하지 않는 challenger 모델이다. 입력 전처리 상태와
가중치를 함께 ``joblib``으로 직렬화해 ``CTRModel``의 기본 저장 계약을 사용한다.

범주형 입력은 학습 데이터에서 관측한 vocabulary와 별도의 UNKNOWN bucket으로
one-hot 인코딩한다. 인코딩은 전체 데이터셋을 한꺼번에 만들지 않고 미니배치마다
수행한다. 자유 텍스트처럼 cardinality가 큰 범주형 컬럼이 있어도 dense 표현의
메모리 사용량이 batch size에 비례하도록 하기 위함이다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Any

import numpy as np
import pandas as pd

from src.models.base import CTRModel


class MLPModel(CTRModel):
    """ReLU 두 층과 sigmoid 출력층을 사용하는 NumPy 이진 분류기."""

    def __init__(
        self,
        scale_pos_weight: float | None = None,
        hidden_layer_sizes: Sequence[int] = (32, 16),
        epochs: int = 200,
        learning_rate: float = 0.001,
        batch_size: int = 256,
        l2: float = 1e-4,
        random_state: int = 42,
    ) -> None:
        hidden = tuple(int(size) for size in hidden_layer_sizes)
        if len(hidden) != 2 or any(size <= 0 for size in hidden):
            raise ValueError("hidden_layer_sizes는 양의 정수 두 개여야 합니다")
        if epochs <= 0:
            raise ValueError("epochs는 양수여야 합니다")
        if batch_size <= 0:
            raise ValueError("batch_size는 양수여야 합니다")
        if learning_rate <= 0 or not np.isfinite(learning_rate):
            raise ValueError("learning_rate는 유한한 양수여야 합니다")
        if l2 < 0 or not np.isfinite(l2):
            raise ValueError("l2는 유한한 음이 아닌 값이어야 합니다")

        self.scale_pos_weight = scale_pos_weight
        self.hidden_layer_sizes = hidden
        self.epochs = int(epochs)
        self.learning_rate = float(learning_rate)
        self.batch_size = int(batch_size)
        self.l2 = float(l2)
        self.random_state = int(random_state)

        self.feature_columns_: tuple[str, ...] | None = None
        self.categorical_features_: tuple[str, ...] = ()
        self.numeric_features_: tuple[str, ...] = ()
        self.categorical_vocabulary_: dict[str, tuple[Any, ...]] = {}
        self.category_maps_: dict[str, dict[tuple[str, Any] | None, int]] = {}
        self.category_offsets_: dict[str, tuple[int, int]] = {}
        self.numeric_means_: dict[str, float] = {}
        self.numeric_scales_: dict[str, float] = {}
        self.input_dim_: int | None = None
        self.positive_weight_: float | None = None
        self.weights_: list[np.ndarray] = []
        self.biases_: list[np.ndarray] = []
        self._fitted = False

    @staticmethod
    def _category_key(value: object) -> tuple[str, Any] | None:
        """범주 값을 type-aware하고 hashable한 key로 정규화한다."""
        if value is None:
            return None
        missing = pd.isna(value)
        if isinstance(missing, (bool, np.bool_)) and bool(missing):
            return None
        if isinstance(value, np.generic):
            value = value.item()
        try:
            hash(value)
        except TypeError:
            value = repr(value)
        return type(value).__qualname__, value

    @staticmethod
    def _categorical_columns(
        X_train: pd.DataFrame, categorical_features: Sequence[str] | None
    ) -> tuple[str, ...]:
        if categorical_features is None:
            return tuple(
                column
                for column in X_train.columns
                if (
                    pd.api.types.is_object_dtype(X_train[column])
                    or isinstance(X_train[column].dtype, pd.CategoricalDtype)
                )
            )
        columns = tuple(categorical_features)
        duplicated = {column for column in columns if columns.count(column) > 1}
        if duplicated:
            raise ValueError(f"categorical_features에 중복 컬럼이 있습니다: {sorted(duplicated)}")
        missing = [column for column in columns if column not in X_train.columns]
        if missing:
            raise ValueError(f"categorical_features가 입력에 없습니다: {missing}")
        return columns

    def _fit_encoder(
        self, X_train: pd.DataFrame, categorical_features: Sequence[str] | None
    ) -> None:
        self.feature_columns_ = tuple(str(column) for column in X_train.columns)
        self.categorical_features_ = self._categorical_columns(X_train, categorical_features)
        categorical_set = set(self.categorical_features_)
        self.numeric_features_ = tuple(
            column for column in self.feature_columns_ if column not in categorical_set
        )

        self.categorical_vocabulary_ = {}
        self.category_maps_ = {}
        self.category_offsets_ = {}
        self.numeric_means_ = {}
        self.numeric_scales_ = {}

        offset = len(self.numeric_features_)
        for column in self.categorical_features_:
            vocabulary: list[Any] = []
            mapping: dict[tuple[str, Any] | None, int] = {}
            for value in X_train[column].to_numpy(dtype=object):
                key = self._category_key(value)
                if key is not None and key not in mapping:
                    mapping[key] = len(vocabulary)
                    vocabulary.append(value.item() if isinstance(value, np.generic) else value)

            # 마지막 한 칸은 학습에 없던 값과 결측을 위한 명시적 UNKNOWN bucket이다.
            width = len(vocabulary) + 1
            self.categorical_vocabulary_[column] = tuple(vocabulary)
            self.category_maps_[column] = mapping
            self.category_offsets_[column] = (offset, offset + width)
            offset += width

        for column in self.numeric_features_:
            numeric = np.asarray(
                pd.to_numeric(X_train[column], errors="coerce"), dtype=np.float64
            )
            finite = numeric[np.isfinite(numeric)]
            mean = float(finite.mean()) if len(finite) else 0.0
            scale = float(finite.std()) if len(finite) else 1.0
            if not np.isfinite(scale) or scale <= 0.0:
                scale = 1.0
            self.numeric_means_[column] = mean
            self.numeric_scales_[column] = scale

        self.input_dim_ = offset

    def _numeric_and_category_codes(
        self, X: pd.DataFrame
    ) -> tuple[np.ndarray, dict[str, np.ndarray]]:
        self._ensure_fitted()
        assert self.feature_columns_ is not None
        assert self.input_dim_ is not None

        missing = [column for column in self.feature_columns_ if column not in X.columns]
        if missing:
            raise ValueError(f"모델 입력에 필요한 컬럼이 없습니다: {missing}")
        frame = X.loc[:, self.feature_columns_]
        numeric_matrix = np.empty((len(frame), len(self.numeric_features_)), dtype=np.float32)

        for index, column in enumerate(self.numeric_features_):
            numeric = np.asarray(
                pd.to_numeric(frame[column], errors="coerce"), dtype=np.float32
            )
            mean = self.numeric_means_[column]
            scale = self.numeric_scales_[column]
            invalid = ~np.isfinite(numeric)
            if invalid.any():
                numeric[invalid] = mean
            numeric_matrix[:, index] = (numeric - mean) / scale

        category_codes: dict[str, np.ndarray] = {}
        for column in self.categorical_features_:
            start, end = self.category_offsets_[column]
            unknown_index = end - start - 1
            mapping = self.category_maps_[column]
            category_codes[column] = np.fromiter(
                (mapping.get(self._category_key(value), unknown_index)
                 for value in frame[column].to_numpy(dtype=object)),
                dtype=np.intp,
                count=len(frame),
            )
        return numeric_matrix, category_codes

    def _encode(self, X: pd.DataFrame) -> np.ndarray:
        """입력을 dense one-hot/표준화 행렬로 materialize한다."""
        numeric_matrix, category_codes = self._numeric_and_category_codes(X)
        assert self.input_dim_ is not None
        encoded = np.zeros((len(X), self.input_dim_), dtype=np.float32)
        encoded[:, : len(self.numeric_features_)] = numeric_matrix
        row_indices = np.arange(len(X))
        for column, codes in category_codes.items():
            start, _ = self.category_offsets_[column]
            encoded[row_indices, start + codes] = 1.0
        return encoded

    def _forward_compact(
        self, numeric_matrix: np.ndarray, category_codes: dict[str, np.ndarray]
    ) -> tuple[np.ndarray, np.ndarray, np.ndarray, np.ndarray, np.ndarray]:
        """one-hot 행렬을 materialize하지 않는 one-hot 동치 forward pass."""
        first_weights = self.weights_[0]
        first_bias = self.biases_[0]
        numeric_width = len(self.numeric_features_)
        first_pre_activation = numeric_matrix @ first_weights[:numeric_width] + first_bias
        for column, codes in category_codes.items():
            start, _ = self.category_offsets_[column]
            first_pre_activation += first_weights[start + codes]
        first_activation = np.maximum(first_pre_activation, 0.0)

        second_pre_activation = first_activation @ self.weights_[1] + self.biases_[1]
        second_activation = np.maximum(second_pre_activation, 0.0)
        logits = second_activation @ self.weights_[2] + self.biases_[2]
        clipped = np.clip(logits, -40.0, 40.0)
        probabilities = 1.0 / (1.0 + np.exp(-clipped))
        return (
            first_activation,
            second_activation,
            first_pre_activation,
            second_pre_activation,
            probabilities,
        )

    def transform(self, X: pd.DataFrame) -> np.ndarray:
        """학습 시 저장한 전처리 상태로 입력을 one-hot/표준화한다."""
        return self._encode(X)

    def _ensure_fitted(self) -> None:
        if not self._fitted:
            raise ValueError("모델이 학습되지 않았습니다.")

    def _forward(self, encoded: np.ndarray) -> tuple[list[np.ndarray], list[np.ndarray]]:
        activations = [encoded]
        pre_activations: list[np.ndarray] = []
        for layer, (weights, bias) in enumerate(zip(self.weights_, self.biases_, strict=True)):
            pre_activation = activations[-1] @ weights + bias
            pre_activations.append(pre_activation)
            if layer == len(self.weights_) - 1:
                clipped = np.clip(pre_activation, -40.0, 40.0)
                activation = 1.0 / (1.0 + np.exp(-clipped))
            else:
                activation = np.maximum(pre_activation, 0.0)
            activations.append(activation)
        return activations, pre_activations

    def _initialize_parameters(self, rng: np.random.Generator) -> None:
        assert self.input_dim_ is not None
        layer_sizes = (self.input_dim_, *self.hidden_layer_sizes, 1)
        self.weights_ = [
            rng.normal(
                0.0,
                np.sqrt(2.0 / fan_in),
                size=(fan_in, fan_out),
            ).astype(np.float32)
            for fan_in, fan_out in zip(layer_sizes[:-1], layer_sizes[1:], strict=True)
        ]
        self.biases_ = [np.zeros((size,), dtype=np.float32) for size in layer_sizes[1:]]

    def fit(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        categorical_features: Sequence[str] | None = None,
    ) -> None:
        """미니배치 Adam으로 weighted BCE를 최소화한다."""
        if len(X_train) != len(y_train):
            raise ValueError("X_train과 y_train의 행 수가 다릅니다")
        y = np.asarray(y_train, dtype=np.float32).reshape(-1)
        if not np.all(np.isin(y, (0.0, 1.0))):
            raise ValueError("y_train은 0과 1의 이진 라벨이어야 합니다")
        positive = int(np.count_nonzero(y == 1.0))
        negative = int(np.count_nonzero(y == 0.0))
        if positive == 0 or negative == 0:
            raise ValueError("weighted BCE를 위해 양성·음성 라벨이 모두 필요합니다")

        self._fit_encoder(X_train, categorical_features)
        self.positive_weight_ = negative / positive
        # scale_pos_weight는 입력으로 받을 수 있지만, MLP의 weighted BCE는 항상
        # 현재 학습 split의 neg/pos를 계산해 사용한다.
        self.scale_pos_weight = self.positive_weight_

        rng = np.random.default_rng(self.random_state)
        self._initialize_parameters(rng)
        # 미니배치 루프가 동일한 encoder/forward 경로를 사용하도록 학습 시작 전에
        # 내부 상태를 준비 완료로 표시한다. 학습 종료 시에도 다시 확인한다.
        self._fitted = True
        first_moments = [np.zeros_like(weights) for weights in self.weights_]
        second_moments = [np.zeros_like(weights) for weights in self.weights_]
        first_bias_moments = [np.zeros_like(bias) for bias in self.biases_]
        second_bias_moments = [np.zeros_like(bias) for bias in self.biases_]
        step = 0
        # 전체 one-hot 행렬은 만들지 않는다. 수치 블록과 category code만 캐시하면
        # 입력 차원이 큰 경우에도 메모리는 원본 DataFrame에 비례해 작게 유지된다.
        all_numeric, all_category_codes = self._numeric_and_category_codes(X_train)

        for _ in range(self.epochs):
            permutation = rng.permutation(len(X_train))
            for start in range(0, len(X_train), self.batch_size):
                batch_indices = permutation[start : start + self.batch_size]
                numeric_matrix = all_numeric[batch_indices]
                category_codes = {
                    column: codes[batch_indices]
                    for column, codes in all_category_codes.items()
                }
                labels = y[batch_indices, None]
                (
                    first_activation,
                    second_activation,
                    first_pre_activation,
                    second_pre_activation,
                    probabilities,
                ) = self._forward_compact(numeric_matrix, category_codes)

                sample_weights = np.where(labels == 1.0, self.positive_weight_, 1.0)
                delta = (probabilities - labels) * sample_weights / len(labels)
                gradients_w: list[np.ndarray] = [np.empty_like(weights) for weights in self.weights_]
                gradients_b: list[np.ndarray] = [np.empty_like(bias) for bias in self.biases_]

                gradients_w[2] = second_activation.T @ delta
                gradients_b[2] = delta.sum(axis=0)
                delta_second = (delta @ self.weights_[2].T) * (second_pre_activation > 0.0)
                gradients_w[1] = first_activation.T @ delta_second
                gradients_b[1] = delta_second.sum(axis=0)
                delta_first = (delta_second @ self.weights_[1].T) * (
                    first_pre_activation > 0.0
                )
                gradients_w[0] = np.zeros_like(self.weights_[0])
                numeric_width = len(self.numeric_features_)
                gradients_w[0][:numeric_width] = numeric_matrix.T @ delta_first
                for column, codes in category_codes.items():
                    start_offset, _ = self.category_offsets_[column]
                    np.add.at(gradients_w[0], start_offset + codes, delta_first)
                gradients_b[0] = delta_first.sum(axis=0)

                step += 1
                bias_correction_1 = 1.0 - 0.9**step
                bias_correction_2 = 1.0 - 0.999**step

                # Hidden/output layers are small and dense, so their regular Adam
                # updates remain vectorized.
                for layer in (1, 2):
                    gradient = gradients_w[layer] + self.l2 * self.weights_[layer]
                    first_moments[layer] = 0.9 * first_moments[layer] + 0.1 * gradient
                    second_moments[layer] = (
                        0.999 * second_moments[layer] + 0.001 * gradient**2
                    )
                    first_bias_moments[layer] = (
                        0.9 * first_bias_moments[layer] + 0.1 * gradients_b[layer]
                    )
                    second_bias_moments[layer] = (
                        0.999 * second_bias_moments[layer]
                        + 0.001 * gradients_b[layer] ** 2
                    )
                    corrected_moment = first_moments[layer] / bias_correction_1
                    corrected_second = second_moments[layer] / bias_correction_2
                    self.weights_[layer] -= self.learning_rate * corrected_moment / (
                        np.sqrt(corrected_second) + 1e-8
                    )
                    corrected_bias_moment = first_bias_moments[layer] / bias_correction_1
                    corrected_bias_second = second_bias_moments[layer] / bias_correction_2
                    self.biases_[layer] -= self.learning_rate * corrected_bias_moment / (
                        np.sqrt(corrected_bias_second) + 1e-8
                    )

                # One-hot category columns are sparse: only rows selected by this
                # batch receive a data gradient. Updating those rows directly avoids
                # a full (vocabulary × hidden) Adam pass for every mini-batch while
                # preserving the one-hot gradient exactly for selected values.
                first_indices = np.arange(numeric_width)
                category_rows = [
                    codes + self.category_offsets_[column][0]
                    for column, codes in category_codes.items()
                ]
                category_indices = (
                    np.unique(np.concatenate(category_rows))
                    if category_rows
                    else np.empty(0, dtype=np.intp)
                )
                first_indices = np.concatenate((first_indices, category_indices))
                if len(first_indices):
                    gradient = gradients_w[0][first_indices] + self.l2 * self.weights_[0][
                        first_indices
                    ]
                    first_moments[0][first_indices] = (
                        0.9 * first_moments[0][first_indices] + 0.1 * gradient
                    )
                    second_moments[0][first_indices] = (
                        0.999 * second_moments[0][first_indices]
                        + 0.001 * gradient**2
                    )
                    corrected_moment = first_moments[0][first_indices] / bias_correction_1
                    corrected_second = second_moments[0][first_indices] / bias_correction_2
                    self.weights_[0][first_indices] -= self.learning_rate * corrected_moment / (
                        np.sqrt(corrected_second) + 1e-8
                    )

                first_bias_moments[0] = 0.9 * first_bias_moments[0] + 0.1 * gradients_b[0]
                second_bias_moments[0] = (
                    0.999 * second_bias_moments[0] + 0.001 * gradients_b[0] ** 2
                )
                corrected_bias_moment = first_bias_moments[0] / bias_correction_1
                corrected_bias_second = second_bias_moments[0] / bias_correction_2
                self.biases_[0] -= self.learning_rate * corrected_bias_moment / (
                    np.sqrt(corrected_bias_second) + 1e-8
                )

        self._fitted = True

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """(n_samples, 2) 형태의 음성·양성 확률을 반환한다."""
        self._ensure_fitted()
        if len(X) == 0:
            return np.empty((0, 2), dtype=np.float64)

        positive_chunks: list[np.ndarray] = []
        for start in range(0, len(X), self.batch_size):
            numeric_matrix, category_codes = self._numeric_and_category_codes(
                X.iloc[start : start + self.batch_size]
            )
            probabilities = self._forward_compact(numeric_matrix, category_codes)[-1][:, 0]
            positive_chunks.append(np.asarray(probabilities, dtype=np.float64))
        positive = np.concatenate(positive_chunks)
        return np.column_stack((1.0 - positive, positive))
