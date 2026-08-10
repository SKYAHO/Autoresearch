"""NumPy 기반 2-layer MLP CTR 모델.

[파이프라인] ``src.pipeline.train``이 LightGBM challenger와 같은 데이터 분할·피처
계약을 사용해 학습할 수 있는 비트리 ``CTRModel`` 구현체를 제공한다. 범주형 입력은
학습 시 관측한 vocabulary를 one-hot으로 펼치고, vocabulary 밖 값은 전용 UNKNOWN
열로 보낸다. 수치형 입력은 학습 통계로 표준화하므로 저장된 모델만으로도
``predict_proba`` 전처리를 재현할 수 있다.

[기능] 은닉층 32·16, ReLU, sigmoid 출력, Adam 최적화, weighted BCE와 L2 정규화를
순수 NumPy로 구현한다. ``CTRModel``의 기본 joblib 저장 계약을 그대로 사용하므로
독립 평가 경로가 저장된 객체의 ``predict_proba``를 직접 호출할 수 있다.

[비책임] 데이터 분할·평가 지표·ONNX 패키징은 이 모듈이 소유하지 않는다. 학습
파이프라인은 MLP를 LightGBM 전용 변환기와 분리해 호출하며, 이 클래스는 학습
입력의 컬럼 순서와 범주형 목록을 호출자로부터 받는다.
"""

from __future__ import annotations

from collections.abc import Sequence
from typing import Optional

import numpy as np
import pandas as pd

from src.models.base import CTRModel


class MLPModel(CTRModel):
    """CTR용 2-layer fully-connected MLP.

    ``hidden_dims``는 두 개의 은닉층 크기이며, 기본값은 이 실험의 고정 challenger
    설정인 ``(32, 16)``이다. 범주형 전처리 상태와 수치형 통계를 객체에 함께 보관해
    joblib round-trip 이후에도 학습과 같은 입력 변환을 수행한다.
    """

    def __init__(
        self,
        hidden_dims: Sequence[int] = (32, 16),
        epochs: int = 200,
        learning_rate: float = 0.001,
        batch_size: int = 256,
        l2: float = 1e-4,
        random_state: int = 42,
        *,
        seed: int | None = None,
        hidden_layers: Sequence[int] | None = None,
    ) -> None:
        """모델 하이퍼파라미터를 설정한다.

        ``seed``와 ``hidden_layers``는 설정 파일·실험 코드에서 자주 쓰는 이름을
        수용하는 호환 별칭이다. 둘 다 주어지면 별칭이 명시적인 인자로 적용된다.
        """
        if hidden_layers is not None:
            hidden_dims = hidden_layers
        if seed is not None:
            random_state = seed

        self.hidden_dims = tuple(int(width) for width in hidden_dims)
        if len(self.hidden_dims) != 2 or any(width <= 0 for width in self.hidden_dims):
            raise ValueError("hidden_dims는 양의 정수 두 개여야 합니다")
        if epochs <= 0:
            raise ValueError("epochs는 양수여야 합니다")
        if not np.isfinite(learning_rate) or learning_rate <= 0:
            raise ValueError("learning_rate는 유한한 양수여야 합니다")
        if batch_size <= 0:
            raise ValueError("batch_size는 양수여야 합니다")
        if not np.isfinite(l2) or l2 < 0:
            raise ValueError("l2는 유한한 음이 아닌 값이어야 합니다")

        self.epochs = int(epochs)
        self.learning_rate = float(learning_rate)
        self.batch_size = int(batch_size)
        self.l2 = float(l2)
        self.random_state = int(random_state)
        # 실험 파라미터를 조회하는 코드가 어느 이름을 사용하더라도 같은 seed를
        # 가리키게 한다.
        self.seed = self.random_state
        self.hidden_layers = self.hidden_dims

        self.feature_columns_: tuple[str, ...] | None = None
        self.categorical_features_: tuple[str, ...] = ()
        self.category_vocabularies_: dict[str, tuple[object, ...]] = {}
        self.numeric_means_: dict[str, float] = {}
        self.numeric_stds_: dict[str, float] = {}
        self.positive_weight_: float | None = None
        self.weights_: list[np.ndarray] | None = None
        self.biases_: list[np.ndarray] | None = None
        self.loss_history_: list[float] = []

    @staticmethod
    def _is_missing(value: object) -> bool:
        """스칼라 category 값의 결측 여부를 안전하게 확인한다."""
        if value is None:
            return True
        try:
            result = pd.isna(value)
        except (TypeError, ValueError):
            return False
        return isinstance(result, (bool, np.bool_)) and bool(result)

    @staticmethod
    def _normalise_scalar(value: object) -> object:
        """NumPy scalar를 Python scalar로 바꿔 train/predict key를 맞춘다."""
        if isinstance(value, np.generic):
            return value.item()
        return value

    @classmethod
    def _category_key(cls, value: object) -> tuple[object, ...]:
        """범주 값의 타입을 포함한 hashable key를 만든다."""
        value = cls._normalise_scalar(value)
        if cls._is_missing(value):
            return ("<missing>",)
        try:
            hash(value)
        except TypeError:
            return (type(value).__name__, repr(value))
        return (type(value).__name__, value)

    @classmethod
    def _observed_categories(cls, series: pd.Series) -> tuple[object, ...]:
        """series에서 결측을 제외한 vocabulary를 처음 관측한 순서로 반환한다."""
        values = pd.unique(series.dropna()).tolist()
        vocabulary: list[object] = []
        seen: set[tuple[object, ...]] = set()
        for value in values:
            value = cls._normalise_scalar(value)
            if cls._is_missing(value):
                continue
            key = cls._category_key(value)
            if key not in seen:
                seen.add(key)
                vocabulary.append(value)
        return tuple(vocabulary)

    @staticmethod
    def _as_numeric(series: pd.Series, column: str) -> np.ndarray:
        """수치형 series를 float64로 바꾸고, 변환 불가능한 값은 명시적으로 거부한다."""
        try:
            values = series.to_numpy(dtype=np.float64, na_value=np.nan)
        except (TypeError, ValueError) as error:
            raise ValueError(f"수치형 피처 {column!r}를 float로 변환할 수 없습니다") from error
        return values

    @staticmethod
    def _sigmoid(logits: np.ndarray) -> np.ndarray:
        """overflow 없이 sigmoid를 계산한다."""
        return 1.0 / (1.0 + np.exp(-np.clip(logits, -50.0, 50.0)))

    def _encode_features(self, features: pd.DataFrame, *, fit: bool) -> np.ndarray:
        """저장된 전처리 계약으로 DataFrame을 dense NumPy 행렬로 인코딩한다."""
        if fit:
            feature_columns = tuple(str(column) for column in features.columns)
            if len(set(feature_columns)) != len(feature_columns):
                raise ValueError("모델 입력 컬럼은 중복될 수 없습니다")
            self.feature_columns_ = feature_columns
        elif self.feature_columns_ is None:
            raise ValueError("모델이 학습되지 않았습니다.")

        feature_columns = self.feature_columns_
        assert feature_columns is not None
        missing_columns = [column for column in feature_columns if column not in features]
        if missing_columns:
            raise ValueError(f"모델 입력 피처가 없습니다: {missing_columns}")
        frame = features.loc[:, feature_columns]
        blocks: list[np.ndarray] = []

        for column in feature_columns:
            series = frame[column]
            if column in self.categorical_features_:
                vocabulary = self.category_vocabularies_[column]
                unknown_index = len(vocabulary)
                encoded = np.full(len(series), unknown_index, dtype=np.int64)
                mapping = {
                    self._category_key(value): index for index, value in enumerate(vocabulary)
                }
                values = series.to_numpy(dtype=object)
                for row_index, value in enumerate(values):
                    encoded[row_index] = mapping.get(self._category_key(value), unknown_index)
                one_hot = np.zeros(
                    (len(series), len(vocabulary) + 1),
                    dtype=np.float64,
                )
                if len(series):
                    one_hot[np.arange(len(series)), encoded] = 1.0
                blocks.append(one_hot)
                continue

            values = self._as_numeric(series, column)
            if fit:
                finite = np.isfinite(values)
                if not finite.any():
                    raise ValueError(f"수치형 피처 {column!r}에 유효한 값이 없습니다")
                mean = float(np.mean(values[finite]))
                std = float(np.std(values[finite]))
                self.numeric_means_[column] = mean
                self.numeric_stds_[column] = std if std > 1e-12 else 1.0
            mean = self.numeric_means_[column]
            std = self.numeric_stds_[column]
            values = np.where(np.isfinite(values), values, mean)
            blocks.append(((values - mean) / std).reshape(-1, 1))

        if not blocks:
            return np.empty((len(frame), 0), dtype=np.float64)
        return np.concatenate(blocks, axis=1)

    def fit(
        self,
        X_train: pd.DataFrame,
        y_train: pd.Series,
        categorical_features: Optional[list] = None,
    ) -> None:
        """weighted BCE와 Adam으로 MLP를 학습한다."""
        if len(X_train) != len(y_train):
            raise ValueError("X_train과 y_train의 행 수가 다릅니다")
        if len(X_train) == 0:
            raise ValueError("학습 데이터가 비어 있습니다")

        y = np.asarray(y_train, dtype=np.float64).reshape(-1)
        if not np.isfinite(y).all() or not np.isin(y, (0.0, 1.0)).all():
            raise ValueError("y_train은 0/1 이진 라벨이어야 합니다")
        positive_count = int(np.sum(y == 1.0))
        negative_count = int(np.sum(y == 0.0))
        if positive_count == 0 or negative_count == 0:
            raise ValueError("weighted BCE를 위해 양성·음성 라벨이 모두 필요합니다")

        if categorical_features is None:
            categorical = tuple(
                column
                for column in X_train.columns
                if isinstance(X_train[column].dtype, pd.CategoricalDtype)
                or pd.api.types.is_object_dtype(X_train[column].dtype)
                or pd.api.types.is_string_dtype(X_train[column].dtype)
            )
        else:
            categorical = tuple(str(column) for column in categorical_features)
        missing_categorical = [column for column in categorical if column not in X_train]
        if missing_categorical:
            raise ValueError(f"범주형 피처가 입력에 없습니다: {missing_categorical}")
        self.categorical_features_ = categorical
        self.category_vocabularies_ = {
            column: self._observed_categories(X_train[column]) for column in categorical
        }
        self.numeric_means_ = {}
        self.numeric_stds_ = {}
        self.feature_columns_ = None
        matrix = self._encode_features(X_train, fit=True)
        if matrix.shape[1] == 0:
            raise ValueError("학습 입력 피처가 없습니다")

        self.positive_weight_ = negative_count / positive_count
        sample_weights = np.where(y == 1.0, self.positive_weight_, 1.0)
        rng = np.random.default_rng(self.random_state)
        layer_sizes = (matrix.shape[1], *self.hidden_dims, 1)
        self.weights_ = [
            rng.normal(
                0.0,
                np.sqrt(2.0 / layer_sizes[index]),
                size=(layer_sizes[index + 1], layer_sizes[index]),
            ).astype(np.float64)
            for index in range(len(layer_sizes) - 1)
        ]
        self.biases_ = [
            np.zeros(layer_sizes[index + 1], dtype=np.float64)
            for index in range(len(layer_sizes) - 1)
        ]
        first_moment_weights = [np.zeros_like(weight) for weight in self.weights_]
        second_moment_weights = [np.zeros_like(weight) for weight in self.weights_]
        first_moment_biases = [np.zeros_like(bias) for bias in self.biases_]
        second_moment_biases = [np.zeros_like(bias) for bias in self.biases_]
        self.loss_history_ = []
        timestep = 0

        for _ in range(self.epochs):
            order = rng.permutation(len(matrix))
            for start in range(0, len(order), self.batch_size):
                batch_indices = order[start : start + self.batch_size]
                batch_x = matrix[batch_indices]
                batch_y = y[batch_indices]
                batch_weights = sample_weights[batch_indices]
                activations, pre_activations = self._forward(batch_x)
                logits = pre_activations[-1].reshape(-1)
                delta = (
                    self._sigmoid(logits) - batch_y
                ).reshape(-1, 1) * batch_weights.reshape(-1, 1) / len(batch_indices)

                gradients_w = [np.empty_like(weight) for weight in self.weights_]
                gradients_b = [np.empty_like(bias) for bias in self.biases_]
                for layer in range(len(self.weights_) - 1, -1, -1):
                    gradients_w[layer] = delta.T @ activations[layer] + self.l2 * self.weights_[layer]
                    gradients_b[layer] = delta.sum(axis=0)
                    if layer:
                        delta = (delta @ self.weights_[layer]) * (
                            pre_activations[layer - 1] > 0.0
                        )

                timestep += 1
                self._adam_update(
                    gradients_w,
                    gradients_b,
                    first_moment_weights,
                    second_moment_weights,
                    first_moment_biases,
                    second_moment_biases,
                    timestep,
                )

            epoch_logits = self._forward(matrix)[1][-1].reshape(-1)
            self.loss_history_.append(self._loss(epoch_logits, y, sample_weights))

    def _forward(
        self, matrix: np.ndarray
    ) -> tuple[list[np.ndarray], list[np.ndarray]]:
        """은닉 activation과 각 층의 pre-activation을 계산한다."""
        if self.weights_ is None or self.biases_ is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        activations = [matrix]
        pre_activations: list[np.ndarray] = []
        activation = matrix
        for layer, (weight, bias) in enumerate(zip(self.weights_, self.biases_, strict=True)):
            pre_activation = activation @ weight.T + bias
            pre_activations.append(pre_activation)
            activation = (
                np.maximum(pre_activation, 0.0)
                if layer < len(self.weights_) - 1
                else pre_activation
            )
            activations.append(activation)
        return activations, pre_activations

    def _adam_update(
        self,
        gradients_w: list[np.ndarray],
        gradients_b: list[np.ndarray],
        first_moment_weights: list[np.ndarray],
        second_moment_weights: list[np.ndarray],
        first_moment_biases: list[np.ndarray],
        second_moment_biases: list[np.ndarray],
        timestep: int,
    ) -> None:
        """Adam 한 step을 적용한다."""
        if self.weights_ is None or self.biases_ is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        beta1 = 0.9
        beta2 = 0.999
        epsilon = 1e-8
        correction1 = 1.0 - beta1**timestep
        correction2 = 1.0 - beta2**timestep
        for index, (weight, gradient) in enumerate(zip(self.weights_, gradients_w, strict=True)):
            first_moment_weights[index] = beta1 * first_moment_weights[index] + (1 - beta1) * gradient
            second_moment_weights[index] = beta2 * second_moment_weights[index] + (1 - beta2) * gradient**2
            first_hat = first_moment_weights[index] / correction1
            second_hat = second_moment_weights[index] / correction2
            self.weights_[index] = weight - self.learning_rate * first_hat / (
                np.sqrt(second_hat) + epsilon
            )
        for index, (bias, gradient) in enumerate(zip(self.biases_, gradients_b, strict=True)):
            first_moment_biases[index] = beta1 * first_moment_biases[index] + (1 - beta1) * gradient
            second_moment_biases[index] = beta2 * second_moment_biases[index] + (1 - beta2) * gradient**2
            first_hat = first_moment_biases[index] / correction1
            second_hat = second_moment_biases[index] / correction2
            self.biases_[index] = bias - self.learning_rate * first_hat / (
                np.sqrt(second_hat) + epsilon
            )

    def _loss(
        self, logits: np.ndarray, labels: np.ndarray, sample_weights: np.ndarray
    ) -> float:
        """weighted BCE와 L2 penalty를 계산한다."""
        if self.weights_ is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        data_loss = np.mean(
            sample_weights * (np.logaddexp(0.0, logits) - labels * logits)
        )
        regularization = 0.5 * self.l2 * sum(
            float(np.sum(weight**2)) for weight in self.weights_
        )
        return float(data_loss + regularization)

    def predict_proba(self, X: pd.DataFrame) -> np.ndarray:
        """``(n_samples, 2)`` 형태의 [P(0), P(1)]을 반환한다."""
        if self.weights_ is None or self.biases_ is None or self.feature_columns_ is None:
            raise ValueError("모델이 학습되지 않았습니다.")
        matrix = self._encode_features(X, fit=False)
        logits = self._forward(matrix)[1][-1].reshape(-1)
        positive = self._sigmoid(logits)
        return np.column_stack((1.0 - positive, positive))

    def predict(self, X: pd.DataFrame) -> np.ndarray:
        """0.5 threshold를 사용한 이진 예측을 반환한다."""
        return (self.predict_proba(X)[:, 1] >= 0.5).astype(np.int64)
