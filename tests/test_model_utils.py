from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import pytest

from src.models.lgbm_model import LGBMModel
from src.utils.model_utils import (
    convert_lgbm_to_onnx,
    load_categorical_columns,
    save_categorical_columns,
)


def test_categorical_columns_roundtrip(tmp_path: Path) -> None:
    categories_by_column = {
        "category_id": [10, 20, 30],
        "age_group": ["10s", "20s", "30s"],
    }
    path = tmp_path / "categorical_columns.json"

    save_categorical_columns(categories_by_column, str(path))
    assert '"category_id"' in path.read_text(encoding="utf-8")  # JSON, not pickle
    loaded = load_categorical_columns(str(path))

    assert loaded == categories_by_column


def _fit_lgbm(n: int = 200, seed: int = 3) -> tuple[LGBMModel, pd.DataFrame]:
    rng = np.random.default_rng(seed)
    features = pd.DataFrame(
        {
            "num1": rng.random(n),
            "num2": rng.normal(size=n),
            "num3": rng.integers(0, 100, size=n).astype(float),
        }
    )
    labels = pd.Series((rng.random(n) < 0.3).astype(int))
    model = LGBMModel(scale_pos_weight=1, n_estimators=30, num_leaves=15)
    model.fit(features, labels, categorical_features=[])
    return model, features


def test_convert_lgbm_to_onnx_matches_lightgbm_within_tolerance() -> None:
    # zipmap=False라 확률 출력이 dict 시퀀스가 아니라 (n, 2) 텐서다 — 서빙이 그대로
    # 슬라이싱한다(#179 기본값 zipmap=True의 dict 파싱과 다른 지점). ONNX 예측은 원본
    # LightGBM과 허용오차(1e-4) 내로 동일해야 한다("완전 동일/diff 0.0"이 아니라 수치 허용오차).
    import onnxruntime as ort

    model, features = _fit_lgbm()
    onnx_model = convert_lgbm_to_onnx(model, n_features=features.shape[1])
    session = ort.InferenceSession(onnx_model.SerializeToString())
    outputs = session.run(None, {"input": features.to_numpy(dtype=np.float32)})

    # zipmap=False 계약 고정: probabilities 출력이 (n, 2) 2D 텐서.
    probabilities = next(out for out in outputs if getattr(out, "ndim", 0) == 2)
    assert probabilities.shape == (len(features), 2)

    onnx_positive = probabilities[:, 1]
    lgbm_positive = model.predict_proba(features)[:, 1]
    np.testing.assert_allclose(onnx_positive, lgbm_positive, atol=1e-4)


def test_convert_lgbm_to_onnx_rejects_untrained_model() -> None:
    with pytest.raises(ValueError, match="학습"):
        convert_lgbm_to_onnx(LGBMModel(scale_pos_weight=1), n_features=3)


def test_convert_lgbm_to_onnx_holds_for_large_count_features() -> None:
    # 리뷰 반영(#336): ONNX 입력은 float32인데 joblib 폴백은 float64로 추론한다.
    # channel_view_count(최대 1억)처럼 float32 정확 정수 한계(2^24≈16.7M)를 넘는 대형 카운트
    # 피처가 트리 스플릿 임계 근처에 있으면 float32/float64가 다른 분기를 타 확률이 갈릴 수 있다.
    # 학습 신호가 대형값 임계(6.8천만, float32 한계 초과)에 걸리도록 만든 적대적 케이스에서도
    # ONNX(float32)가 joblib과 허용오차 내로 일치하고 상위 랭킹이 보존되는지 못박는다.
    # (onnxmltools의 LightGBM 변환기는 DoubleTensorType을 거부해 float32가 강제된다 — 대신
    #  LightGBM 스플릿 임계는 bin 경계라 이 스케일에서 float32 반올림 오차보다 훨씬 성글다.)
    import onnxruntime as ort

    rng = np.random.default_rng(11)
    n = 3000
    big = rng.integers(16_000_000, 120_000_000, size=n).astype(float)
    small = rng.random(n)
    features = pd.DataFrame({"big_count": big, "small": small})
    labels = pd.Series(((big > 68_000_000) ^ (small < 0.5)).astype(int))
    model = LGBMModel(scale_pos_weight=1, n_estimators=60, num_leaves=31)
    model.fit(features, labels, categorical_features=[])

    onnx_model = convert_lgbm_to_onnx(model, n_features=2)
    session = ort.InferenceSession(onnx_model.SerializeToString())
    probabilities = next(
        out
        for out in session.run(None, {"input": features.to_numpy(dtype=np.float32)})
        if getattr(out, "ndim", 0) == 2
    )
    onnx_positive = probabilities[:, 1]
    lgbm_positive = model.predict_proba(features)[:, 1]

    np.testing.assert_allclose(onnx_positive, lgbm_positive, atol=1e-4)
    # 상위 100개 랭킹 집합 보존(대형값 float32 반올림이 순위를 흔들지 않음).
    top_onnx = set(np.argsort(-onnx_positive)[:100].tolist())
    top_lgbm = set(np.argsort(-lgbm_positive)[:100].tolist())
    assert top_onnx == top_lgbm
