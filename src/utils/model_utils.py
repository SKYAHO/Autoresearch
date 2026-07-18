"""모델 저장/로드 유틸리티."""

import os
import pickle
import joblib


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
