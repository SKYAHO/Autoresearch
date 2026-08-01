"""서빙 ONNX 전환(#302/#179) 단위·통합 테스트.

핵심 계약: ONNX 어댑터가 학습 LightGBM과 수치 허용오차 내로 동일하고,
manifest로 검증된 ONNX 패키지만 로드하며 calibration 체이닝을 보존한다.
"""

from __future__ import annotations

from pathlib import Path

import numpy as np
import pandas as pd
import gc
import tempfile

import pytest

from src.features.model_contract import (
    CATEGORICAL_FEATURE_COLUMNS,
    MODEL_FEATURE_COLUMNS,
)
from src.models.calibration import DownsamplingCalibrator
from src.models.lgbm_model import LGBMModel
from src.serving.model_loader import LocalModelSettings, ModelArtifactError, load_local_model
from src.serving.onnx_model import OnnxProbabilityModel, validate_onnx_session_contract
from src.serving.schemas import CandidateVideo
from src.serving.service import Reranker
from src.tracking.model_package import ModelPackageManifest, save_manifest
from src.utils.model_utils import (
    convert_lgbm_to_onnx,
    save_categorical_columns,
    save_feature_columns,
)


def _synthetic_contract_frame(n: int, seed: int) -> pd.DataFrame:
    """model_contract의 21개 컬럼을 갖춘 합성 프레임(clicked 제외)."""
    rng = np.random.default_rng(seed)
    return pd.DataFrame(
        {
            "age_group": rng.choice(["10s", "20s", "30s", "40s", "50s+"], size=n),
            "occupation": rng.choice(["Student", "Engineer", "Marketer"], size=n),
            "watch_time_band": rng.choice(["morning", "evening", "night", "unknown"], size=n),
            "recent_click_count_7d": rng.integers(0, 20, size=n).astype(float),
            "recent_view_count_7d": rng.integers(0, 30, size=n).astype(float),
            "recent_watch_time_7d": rng.random(size=n) * 100,
            "recent_like_count_7d": rng.integers(0, 10, size=n).astype(float),
            "historical_category_affinity": rng.choice(["A", "B", "C"], size=n),
            "total_event_count_7d": rng.integers(0, 100, size=n).astype(float),
            "category_id": rng.integers(1, 6, size=n),
            "duration_sec": rng.integers(60, 600, size=n).astype(float),
            "view_count": rng.integers(100, 100000, size=n).astype(float),
            "like_ratio": rng.random(size=n),
            "comment_ratio": rng.random(size=n),
            "days_since_upload": rng.integers(0, 30, size=n).astype(float),
            "channel_subscriber_count": rng.integers(0, 1_000_000, size=n).astype(float),
            "channel_view_count": rng.integers(0, 100_000_000, size=n).astype(float),
            "channel_video_count": rng.integers(0, 10_000, size=n).astype(float),
            "topic_similarity": rng.random(size=n),
            "preferred_category_match": rng.integers(0, 2, size=n).astype(float),
            "historical_category_match": rng.integers(0, 2, size=n).astype(float),
        }
    )[list(MODEL_FEATURE_COLUMNS)]


def _categorical_categories(frame: pd.DataFrame) -> dict:
    return {col: sorted(frame[col].unique().tolist()) for col in CATEGORICAL_FEATURE_COLUMNS}


def _cast_categoricals(frame: pd.DataFrame, categories: dict) -> pd.DataFrame:
    out = frame.copy()
    for col, cats in categories.items():
        out[col] = pd.Categorical(out[col], categories=cats)
    return out


def _fit_contract_model(n: int = 300, seed: int = 5):
    frame = _synthetic_contract_frame(n, seed)
    categories = _categorical_categories(frame)
    train_frame = _cast_categoricals(frame, categories)
    rng = np.random.default_rng(seed + 1)
    labels = pd.Series((rng.random(n) < 0.3).astype(int))
    model = LGBMModel(scale_pos_weight=1, n_estimators=40, num_leaves=15)
    model.fit(train_frame, labels, categorical_features=list(CATEGORICAL_FEATURE_COLUMNS))
    return model, categories


# ── OnnxProbabilityModel 어댑터 수치 동등 ─────────────────────────


def test_onnx_adapter_matches_lgbm_on_categorical_input() -> None:
    # 서빙 경로 재현: category dtype 입력(미학습값 포함 → code -1)에서 ONNX 어댑터가
    # joblib LGBMClassifier와 허용오차 내로 동일. category→정수코드 인코딩 경로 검증.
    import onnxruntime as ort

    model, categories = _fit_contract_model()
    onnx_model = convert_lgbm_to_onnx(model, n_features=len(MODEL_FEATURE_COLUMNS))
    session = ort.InferenceSession(onnx_model.SerializeToString())
    adapter = OnnxProbabilityModel(session, MODEL_FEATURE_COLUMNS)

    serve_frame = _synthetic_contract_frame(50, seed=99)
    # 미학습 카테고리 주입 → NaN → code -1 (조용한 강등 경로).
    serve_frame.loc[serve_frame.index[:5], "occupation"] = "UNSEEN_JOB"
    serve_cast = _cast_categoricals(serve_frame, categories)

    onnx_proba = adapter.predict_proba(serve_cast)
    lgbm_proba = model.predict_proba(serve_cast)

    assert onnx_proba.shape == (len(serve_frame), 2)
    np.testing.assert_allclose(onnx_proba, lgbm_proba, atol=1e-4)


class _Metadata:
    def __init__(self, name: str, type_: str, shape: list[object]) -> None:
        self.name = name
        self.type = type_
        self.shape = shape


class _ContractSession:
    def __init__(self, inputs, outputs) -> None:
        self._inputs = inputs
        self._outputs = outputs

    def get_inputs(self):
        return self._inputs

    def get_outputs(self):
        return self._outputs


def test_onnx_contract_rejects_wrong_input_name() -> None:
    session = _ContractSession(
        [_Metadata("wrong", "tensor(float)", [None, 21])],
        [_Metadata("probabilities", "tensor(float)", [None, 2])],
    )
    with pytest.raises(ValueError, match="input"):
        validate_onnx_session_contract(session, feature_count=21)


def test_onnx_contract_rejects_fixed_batch_dimensions() -> None:
    fixed_input = _ContractSession(
        [_Metadata("input", "tensor(float)", [1, 21])],
        [_Metadata("probabilities", "tensor(float)", [None, 2])],
    )
    with pytest.raises(ValueError, match="batch"):
        validate_onnx_session_contract(fixed_input, feature_count=21)
    fixed_output = _ContractSession(
        [_Metadata("input", "tensor(float)", [None, 21])],
        [_Metadata("probabilities", "tensor(float)", [1, 2])],
    )
    with pytest.raises(ValueError, match="batch"):
        validate_onnx_session_contract(fixed_output, feature_count=21)


def test_onnx_adapter_requests_only_validated_probability_output() -> None:
    class MultiOutputSession(_ContractSession):
        def run(self, output_names, inputs):
            assert output_names == ["probabilities"]
            batch = len(next(iter(inputs.values())))
            return [np.tile([[0.25, 0.75]], (batch, 1)).astype(np.float32)]

    session = MultiOutputSession(
        [_Metadata("input", "tensor(float)", [None, 21])],
        [
            _Metadata("auxiliary", "tensor(int64)", [None, 2]),
            _Metadata("probabilities", "tensor(float)", [None, 2]),
        ],
    )
    model = OnnxProbabilityModel(session, MODEL_FEATURE_COLUMNS)

    frame = _synthetic_contract_frame(2, seed=101)
    result = model.predict_proba(_cast_categoricals(frame, _categorical_categories(frame)))

    np.testing.assert_allclose(result, [[0.25, 0.75], [0.25, 0.75]])


@pytest.mark.parametrize(
    ("input_type", "input_shape", "output_type", "output_shape"),
    [
        ("tensor(double)", [None, 21], "tensor(float)", [None, 2]),
        ("tensor(float)", [None, 20], "tensor(float)", [None, 2]),
        ("tensor(float)", [None, 21], "tensor(double)", [None, 2]),
        ("tensor(float)", [None, 21], "tensor(float)", [None, 1]),
    ],
)
def test_onnx_contract_rejects_incompatible_metadata(
    input_type: str,
    input_shape: list[object],
    output_type: str,
    output_shape: list[object],
) -> None:
    session = _ContractSession(
        [_Metadata("input", input_type, input_shape)],
        [_Metadata("probabilities", output_type, output_shape)],
    )
    with pytest.raises(ValueError, match="ONNX"):
        validate_onnx_session_contract(session, feature_count=21)


def test_onnx_model_owns_temporary_workspace_for_session_lifetime() -> None:
    owner = tempfile.TemporaryDirectory()
    workspace = Path(owner.name)
    session = _ContractSession(
        [_Metadata("input", "tensor(float)", [None, 1])],
        [_Metadata("probabilities", "tensor(float)", [None, 2])],
    )
    model = OnnxProbabilityModel(session, ("feature",), workspace_owner=owner)
    del owner
    gc.collect()
    assert workspace.exists()
    del model
    gc.collect()
    assert not workspace.exists()


# ── 로더: 검증된 ONNX package 전용 ──


def _save_contract_artifacts(tmp_path: Path, model, categories, *, with_onnx: bool):
    onnx_dir = tmp_path / "model_onnx"
    feature_path = tmp_path / "features" / "feature_columns.json"
    categorical_path = tmp_path / "features" / "categorical_columns.json"
    save_feature_columns(list(MODEL_FEATURE_COLUMNS), str(feature_path))
    save_categorical_columns(categories, str(categorical_path))
    onnx_path = onnx_dir / "model.onnx"
    if with_onnx:
        onnx_dir.mkdir(parents=True, exist_ok=True)
        onnx_model = convert_lgbm_to_onnx(model, n_features=len(MODEL_FEATURE_COLUMNS))
        onnx_path.write_bytes(onnx_model.SerializeToString())
    else:
        onnx_dir.mkdir(parents=True, exist_ok=True)
        (onnx_dir / "placeholder").write_bytes(b"not-an-onnx-model")
    manifest = ModelPackageManifest.build(
        sampling_rate=1.0,
        model_onnx=onnx_dir,
        feature_columns=feature_path,
        categorical_columns=categorical_path,
        calibration=None,
    )
    manifest_path = tmp_path / "manifest" / "manifest.json"
    save_manifest(manifest, manifest_path)
    return LocalModelSettings(
        onnx_model_path=onnx_path,
        feature_columns_path=feature_path,
        categorical_columns_path=categorical_path,
        manifest_path=manifest_path,
    )


def _candidates_from_frame(frame: pd.DataFrame) -> list[CandidateVideo]:
    records = frame.to_dict(orient="records")
    return [
        CandidateVideo(video_id=f"v{i}", features=record)
        for i, record in enumerate(records)
    ]


def test_local_loader_requires_onnx_without_joblib_fallback(tmp_path: Path) -> None:
    model, categories = _fit_contract_model()
    onnx_settings = _save_contract_artifacts(tmp_path / "onnx", model, categories, with_onnx=True)
    onnx_reranker = load_local_model(onnx_settings)
    assert isinstance(onnx_reranker.model, OnnxProbabilityModel)
    invalid = _save_contract_artifacts(tmp_path / "invalid", model, categories, with_onnx=True)
    invalid.onnx_model_path.unlink()
    with pytest.raises(ModelArtifactError, match="ONNX"):
        load_local_model(invalid)


def test_local_loader_rejects_non_manifest_onnx_entrypoint(tmp_path: Path) -> None:
    model, categories = _fit_contract_model()
    settings = _save_contract_artifacts(tmp_path, model, categories, with_onnx=True)
    alternate = settings.onnx_model_path.parent / "alternate.onnx"
    alternate.write_bytes(settings.onnx_model_path.read_bytes())
    with pytest.raises(ModelArtifactError, match="entrypoint"):
        load_local_model(
            LocalModelSettings(
                onnx_model_path=alternate,
                feature_columns_path=settings.feature_columns_path,
                categorical_columns_path=settings.categorical_columns_path,
                manifest_path=settings.manifest_path,
            )
        )


def test_onnx_reranker_preserves_calibration_chaining(tmp_path: Path) -> None:
    # ONNX 어댑터로 로드해도 main→calibration 체이닝은 그대로: calibration 적용 점수가
    # raw ONNX 점수를 He 보정한 값과 일치하고, monotonic이라 순위는 불변.
    model, categories = _fit_contract_model()
    settings = _save_contract_artifacts(tmp_path, model, categories, with_onnx=True)
    reranker = load_local_model(settings)
    calibration = DownsamplingCalibrator(0.1)
    calibrated = Reranker(
        model=reranker.model,
        feature_columns=reranker.feature_columns,
        categorical_categories=reranker.categorical_categories,
        calibration=calibration,
    )

    candidates = _candidates_from_frame(_synthetic_contract_frame(30, seed=321))
    raw_items = reranker.rerank(candidates)
    cal_items = calibrated.rerank(candidates)

    # 순위 불변(monotonic).
    assert [i.video_id for i in raw_items] == [i.video_id for i in cal_items]
    # 점수는 raw를 He 보정한 값.
    raw_scores = {i.video_id: i.ctr_score for i in raw_items}
    for item in cal_items:
        expected = float(calibration.calibrate(np.array([raw_scores[item.video_id]]))[0])
        assert item.ctr_score == pytest.approx(expected, abs=1e-9)
