"""모델 저장/로드 + ONNX 변환 유틸리티."""

import json
import os
from collections.abc import Sequence
from typing import Any

import joblib
import numpy as np


def convert_lgbm_to_onnx(model, n_features: int) -> Any:
    """학습된 LGBMModel을 ONNX로 변환한다(#302/#179).

    재학습이 필요 없다 — 카테고리형 컬럼이 pandas category dtype으로 학습됐더라도
    LightGBM은 내부적으로 이미 정수 코드로 스플릿을 구성하므로, ONNX 추론 시 원본
    문자열이 아니라 그 카테고리 순서의 정수 코드(서빙이 `.cat.codes`로 뽑음)를 입력하면
    예측값이 원본과 허용오차 내로 동일하다.

    입력은 컬럼별 다중 입력이 아니라 전체 피처를 이어붙인 단일 float32 텐서
    `[None, n_features]` 하나다(onnxmltools의 LightGBM 변환기가 다중 입력을 지원하지 않음).
    dtype이 float32인 것도 이 변환기의 제약이다 — `DoubleTensorType`은 거부되고
    `Float`/`Int64`만 허용된다. joblib 폴백은 pandas 원본 float64로 추론하므로 이론상
    대형 카운트 피처(float32 정확 정수 한계 2^24≈16.7M 초과)에서 분기가 갈릴 수 있으나,
    LightGBM 스플릿 임계는 bin 경계라 그 스케일에서 float32 반올림 오차보다 훨씬 성글어
    실측상 허용오차 내로 일치한다(tests/test_model_utils.py의 대형값 적대적 케이스로 고정).

    `zipmap=False`로 변환해 확률 출력이 dict 시퀀스가 아니라 `(n, 2)` float 텐서가 되게
    한다 — 서빙(onnxruntime)이 그대로 슬라이싱해 쓰기 위함이다(#179 기본값 zipmap=True와
    다른 지점).

    Args:
        model: 학습된 LGBMModel(src.models.lgbm_model.LGBMModel) 인스턴스.
        n_features: 학습에 사용한 피처 개수(입력 텐서 shape 결정용).

    Returns:
        onnx.ModelProto. 학습 패키지 staging에서 `mlflow.onnx.save_model`로 저장한다.
    """
    from onnxmltools import convert_lightgbm
    from onnxmltools.convert.common.data_types import FloatTensorType

    if model.model is None:
        raise ValueError("모델이 학습되지 않았습니다.")
    initial_type = [("input", FloatTensorType([None, n_features]))]
    return convert_lightgbm(model.model, initial_types=initial_type, zipmap=False)


def convert_mlp_to_onnx(
    model,
    feature_columns: Sequence[str],
    categorical_categories: dict[str, Sequence[object]] | None = None,
) -> Any:
    """학습된 ``MLPModel``을 ONNX로 변환한다.

    LightGBM 변환기(``convert_lgbm_to_onnx``)는 트리 전용이므로 MLP 경로에서
    호출하지 않는다. 이 변환기는 MLP가 joblib 모델에서 수행하는 표준화·one-hot
    전처리와 dense layer를 하나의 그래프로 직렬화한다. ONNX 입력은 기존 서빙
    어댑터가 제공하는 동일한 raw feature matrix다 — categorical 값은 학습
    vocabulary의 category code, vocabulary 밖 값은 ``-1``로 들어온다.

    Args:
        model: 학습된 ``src.models.mlp_model.MLPModel``.
        feature_columns: 학습 피처의 원래 순서.
        categorical_categories: 서빙 metadata가 사용하는 외부 category code 순서.
            생략하면 MLP 내부 vocabulary 순서를 외부 code 순서로 사용한다.

    Returns:
        ``onnx.ModelProto``. 학습 패키지 staging에서 MLflow가 저장한다.
    """
    from onnx import TensorProto, checker, helper, numpy_helper

    if getattr(model, "weights_", None) is None or getattr(model, "biases_", None) is None:
        raise ValueError("모델이 학습되지 않았습니다.")
    if getattr(model, "feature_columns_", None) != tuple(feature_columns):
        raise ValueError("MLP 모델의 feature columns가 변환 입력과 다릅니다")

    feature_columns = tuple(feature_columns)
    categorical_features = tuple(getattr(model, "categorical_features_", ()))
    vocabularies = getattr(model, "category_vocabularies_", {})
    means = getattr(model, "numeric_means_", {})
    standard_deviations = getattr(model, "numeric_stds_", {})
    external_categories = categorical_categories or {
        column: vocabulary for column, vocabulary in vocabularies.items()
    }

    nodes = []
    initializers = []
    transformed_blocks: list[str] = []
    input_name = "input"
    initializer_counter = 0

    def add_initializer(value: np.ndarray, prefix: str) -> str:
        nonlocal initializer_counter
        name = f"{prefix}_{initializer_counter}"
        initializer_counter += 1
        initializers.append(numpy_helper.from_array(np.asarray(value), name=name))
        return name

    category_key = getattr(model, "_category_key", None)

    def same_category(left: object, right: object) -> bool:
        if category_key is not None:
            return category_key(left) == category_key(right)
        return type(left) is type(right) and left == right

    for feature_index, column in enumerate(feature_columns):
        raw_column = f"raw_{feature_index}"
        nodes.append(
            helper.make_node(
                "Gather",
                [
                    input_name,
                    add_initializer(
                        np.asarray([feature_index], dtype=np.int64),
                        f"feature_index_{feature_index}",
                    ),
                ],
                [raw_column],
                axis=1,
            )
        )
        if column in categorical_features:
            vocabulary = tuple(vocabularies[column])
            external = tuple(external_categories.get(column, vocabulary))
            known_indicators: list[str] = []
            for category_index, category in enumerate(vocabulary):
                external_index = next(
                    (
                        index
                        for index, external_value in enumerate(external)
                        if same_category(category, external_value)
                    ),
                    None,
                )
                indicator = f"category_{feature_index}_{category_index}"
                if external_index is None:
                    zero = add_initializer(
                        np.asarray([0.0], dtype=np.float32), f"category_zero_{feature_index}"
                    )
                    nodes.append(helper.make_node("Mul", [raw_column, zero], [indicator]))
                else:
                    code = add_initializer(
                        np.asarray([external_index], dtype=np.float32),
                        f"category_code_{feature_index}_{category_index}",
                    )
                    equal = f"category_equal_{feature_index}_{category_index}"
                    nodes.append(helper.make_node("Equal", [raw_column, code], [equal]))
                    nodes.append(
                        helper.make_node(
                            "Cast", [equal], [indicator], to=TensorProto.FLOAT
                        )
                    )
                known_indicators.append(indicator)

            known_sum = f"category_known_sum_{feature_index}"
            if not known_indicators:
                zero = add_initializer(
                    np.asarray([0.0], dtype=np.float32), f"category_zero_{feature_index}"
                )
                zero_indicator = f"category_zero_indicator_{feature_index}"
                nodes.append(
                    helper.make_node("Mul", [raw_column, zero], [zero_indicator])
                )
                known_indicators.append(zero_indicator)
            axes = add_initializer(
                np.asarray([1], dtype=np.int64), f"category_axes_{feature_index}"
            )
            known_matrix = f"category_known_matrix_{feature_index}"
            nodes.append(
                helper.make_node(
                    "Concat", known_indicators, [known_matrix], axis=1
                )
            )
            nodes.append(
                helper.make_node(
                    "ReduceSum", [known_matrix, axes], [known_sum], keepdims=1
                )
            )
            one = add_initializer(
                np.asarray([1.0], dtype=np.float32), f"category_one_{feature_index}"
            )
            unknown = f"category_unknown_{feature_index}"
            nodes.append(helper.make_node("Sub", [one, known_sum], [unknown]))
            block = f"categorical_block_{feature_index}"
            nodes.append(
                helper.make_node(
                    "Concat", [*known_indicators, unknown], [block], axis=1
                )
            )
        else:
            mean = add_initializer(
                np.asarray([means[column]], dtype=np.float32), f"numeric_mean_{feature_index}"
            )
            standard_deviation = add_initializer(
                np.asarray([standard_deviations[column]], dtype=np.float32),
                f"numeric_std_{feature_index}",
            )
            centred = f"numeric_centred_{feature_index}"
            nodes.append(helper.make_node("Sub", [raw_column, mean], [centred]))
            block = f"numeric_block_{feature_index}"
            nodes.append(
                helper.make_node("Div", [centred, standard_deviation], [block])
            )
        transformed_blocks.append(block)

    transformed = "transformed_features"
    nodes.append(helper.make_node("Concat", transformed_blocks, [transformed], axis=1))
    activation = transformed
    for layer, (weight, bias) in enumerate(
        zip(model.weights_, model.biases_, strict=True)
    ):
        weight_name = add_initializer(
            np.asarray(weight.T, dtype=np.float32), f"dense_weight_{layer}"
        )
        bias_name = add_initializer(
            np.asarray(bias, dtype=np.float32), f"dense_bias_{layer}"
        )
        product = f"dense_product_{layer}"
        affine = f"dense_affine_{layer}"
        nodes.append(helper.make_node("MatMul", [activation, weight_name], [product]))
        nodes.append(helper.make_node("Add", [product, bias_name], [affine]))
        if layer < len(model.weights_) - 1:
            activation = f"dense_relu_{layer}"
            nodes.append(helper.make_node("Relu", [affine], [activation]))
        else:
            activation = f"dense_sigmoid_{layer}"
            nodes.append(helper.make_node("Sigmoid", [affine], [activation]))

    one = add_initializer(np.asarray([1.0], dtype=np.float32), "probability_one")
    negative_probability = "negative_probability"
    nodes.append(helper.make_node("Sub", [one, activation], [negative_probability]))
    probabilities = "probabilities"
    nodes.append(
        helper.make_node(
            "Concat", [negative_probability, activation], [probabilities], axis=1
        )
    )

    graph = helper.make_graph(
        nodes,
        "ctr_mlp",
        [helper.make_tensor_value_info(input_name, TensorProto.FLOAT, [None, len(feature_columns)])],
        [helper.make_tensor_value_info(probabilities, TensorProto.FLOAT, [None, 2])],
        initializer=initializers,
    )
    onnx_model = helper.make_model(
        graph,
        producer_name="autoresearch-ctr-mlp",
        opset_imports=[helper.make_opsetid("", 13)],
    )
    checker.check_model(onnx_model)
    return onnx_model


def save_model(model, path: str) -> None:
    """
    모델을 joblib 형식으로 저장.

    Args:
        model: 저장할 모델 객체.
        path: 저장 경로.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    joblib.dump(model, path)
    print(f"[저장 완료] {path}")


def load_model(path: str):
    """
    joblib 형식의 모델 로드.

    Args:
        path: 로드 경로.

    Returns:
        로드된 모델 객체.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"모델 파일을 찾을 수 없습니다: {path}")
    model = joblib.load(path)
    print(f"[로드 완료] {path}")
    return model


def save_feature_columns(columns: list, path: str) -> None:
    """
    Feature 컬럼 목록을 JSON 형식으로 저장.

    pickle 대신 JSON을 쓴다 — 역직렬화 시 임의 코드 실행 위험을 없애기 위함이다.

    Args:
        columns: 컬럼 이름 리스트.
        path: 저장 경로.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(columns, f, ensure_ascii=False)
    print(f"[저장 완료] feature_columns: {path}")


def load_feature_columns(path: str) -> list:
    """
    JSON 형식의 feature 컬럼 목록 로드.

    Args:
        path: 로드 경로.

    Returns:
        컬럼 이름 리스트.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Feature 컬럼 파일을 찾을 수 없습니다: {path}")
    with open(path, encoding="utf-8") as f:
        columns = json.load(f)
    print(f"[로드 완료] feature_columns: {path} ({len(columns)} columns)")
    return columns


def save_categorical_columns(categories_by_column: dict, path: str) -> None:
    """
    범주형 컬럼별 카테고리 목록을 JSON 형식으로 저장.

    서빙이 학습과 동일한 category 코드 매핑을 재현하는 데 사용한다. pickle 대신
    JSON을 쓴다 — 역직렬화 시 임의 코드 실행 위험을 없애기 위함이다.

    Args:
        categories_by_column: 컬럼명 -> 학습 시점 카테고리 리스트(순서 보존).
        path: 저장 경로.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "w", encoding="utf-8") as f:
        json.dump(categories_by_column, f, ensure_ascii=False)
    print(f"[저장 완료] categorical_columns: {path}")


def load_categorical_columns(path: str) -> dict:
    """
    JSON 형식의 범주형 카테고리 목록 로드.

    Args:
        path: 로드 경로.

    Returns:
        컬럼명 -> 카테고리 리스트 dict.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Categorical 컬럼 파일을 찾을 수 없습니다: {path}")
    with open(path, encoding="utf-8") as f:
        categories_by_column = json.load(f)
    print(f"[로드 완료] categorical_columns: {path} ({len(categories_by_column)} columns)")
    return categories_by_column
