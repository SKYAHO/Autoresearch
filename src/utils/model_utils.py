"""모델 저장/로드 + ONNX 변환 유틸리티."""

import os
import pickle
from typing import Any

import joblib


def convert_lgbm_to_onnx(model, n_features: int) -> Any:
    """학습된 LGBMModel을 ONNX로 변환한다(#302/#179).

    재학습이 필요 없다 — 카테고리형 컬럼이 pandas category dtype으로 학습됐더라도
    LightGBM은 내부적으로 이미 정수 코드로 스플릿을 구성하므로, ONNX 추론 시 원본
    문자열이 아니라 그 카테고리 순서의 정수 코드(서빙이 `.cat.codes`로 뽑음)를 입력하면
    예측값이 원본과 허용오차 내로 동일하다.

    입력은 컬럼별 다중 입력이 아니라 전체 피처를 이어붙인 단일 float32 텐서
    `[None, n_features]` 하나다(onnxmltools의 LightGBM 변환기가 다중 입력을 지원하지 않음).

    `zipmap=False`로 변환해 확률 출력이 dict 시퀀스가 아니라 `(n, 2)` float 텐서가 되게
    한다 — 서빙(onnxruntime)이 그대로 슬라이싱해 쓰기 위함이다(#179 기본값 zipmap=True와
    다른 지점).

    Args:
        model: 학습된 LGBMModel(src.models.lgbm_model.LGBMModel) 인스턴스.
        n_features: 학습에 사용한 피처 개수(입력 텐서 shape 결정용).

    Returns:
        onnx.ModelProto. `src.tracking.logger.log_onnx_model`로 기록할 수 있다.
    """
    from onnxmltools import convert_lightgbm
    from onnxmltools.convert.common.data_types import FloatTensorType

    if model.model is None:
        raise ValueError("모델이 학습되지 않았습니다.")
    initial_type = [("input", FloatTensorType([None, n_features]))]
    return convert_lightgbm(model.model, initial_types=initial_type, zipmap=False)


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
    Feature 컬럼 목록을 pickle 형식으로 저장.

    Args:
        columns: 컬럼 이름 리스트.
        path: 저장 경로.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(columns, f)
    print(f"[저장 완료] feature_columns: {path}")


def load_feature_columns(path: str) -> list:
    """
    pickle 형식의 feature 컬럼 목록 로드.

    Args:
        path: 로드 경로.

    Returns:
        컬럼 이름 리스트.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Feature 컬럼 파일을 찾을 수 없습니다: {path}")
    with open(path, "rb") as f:
        columns = pickle.load(f)
    print(f"[로드 완료] feature_columns: {path} ({len(columns)} columns)")
    return columns


def save_categorical_columns(categories_by_column: dict, path: str) -> None:
    """
    범주형 컬럼별 카테고리 목록을 pickle 형식으로 저장.

    서빙이 학습과 동일한 category 코드 매핑을 재현하는 데 사용한다.

    Args:
        categories_by_column: 컬럼명 -> 학습 시점 카테고리 리스트(순서 보존).
        path: 저장 경로.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(categories_by_column, f)
    print(f"[저장 완료] categorical_columns: {path}")


def load_categorical_columns(path: str) -> dict:
    """
    pickle 형식의 범주형 카테고리 목록 로드.

    Args:
        path: 로드 경로.

    Returns:
        컬럼명 -> 카테고리 리스트 dict.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Categorical 컬럼 파일을 찾을 수 없습니다: {path}")
    with open(path, "rb") as f:
        categories_by_column = pickle.load(f)
    print(f"[로드 완료] categorical_columns: {path} ({len(categories_by_column)} columns)")
    return categories_by_column
