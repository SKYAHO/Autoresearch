"""build_training_dataset의 --assembly-source feast 경로 단위 테스트 (#358).

실제 feast/BigQuery 없이 glue만 검증한다: 인자 검증, spine→조회→CSV 컬럼 선택,
누락 피처 가드. feast 조회 자체는 tests/test_feast_retrieval_integration_feast.py가
실물(로컬 File store)로 검증한다.
"""

import pandas as pd
import pytest

from feature_repo import bootstrap
from src.features import feast_retrieval
from src.features.model_contract import MODEL_FEATURE_COLUMNS
from src.pipeline import build_training_dataset as btd


def test_feast_requires_bigquery_source_and_dates() -> None:
    with pytest.raises(ValueError, match="assembly_source='feast'"):
        btd.main(assembly_source="feast", events_source="csv")


def test_apply_cold_start_defaults_fills_nulls_serving_rule() -> None:
    # 영상 미발견 등으로 null인 피처를 서빙과 같은 규칙(카테고리→'unknown', 수치→0)으로 채운다.
    df = pd.DataFrame(
        {
            "category_id": ["Gaming", None],  # categorical
            "view_count": [100, None],  # 수치
            "topic_similarity": [0.9, None],  # 수치(float)
            "clicked": [1, 0],  # 모델 피처 아님 → 안 건드림
        }
    )
    out = feast_retrieval.apply_cold_start_defaults(df)
    assert out.loc[1, "category_id"] == "unknown"
    assert out.loc[1, "view_count"] == 0
    assert out.loc[1, "topic_similarity"] == 0.0
    # 비-null과 비-모델 컬럼은 보존.
    assert out.loc[0, "category_id"] == "Gaming"
    assert out.loc[0, "topic_similarity"] == 0.9
    assert out["clicked"].tolist() == [1, 0]


def _fake_env(monkeypatch, features: pd.DataFrame) -> None:
    spine = pd.DataFrame(
        [{"user_id": "u1", "video_id": "v1",
          "event_timestamp": pd.Timestamp("2026-07-02", tz="UTC"), "clicked": 1}]
    )
    monkeypatch.setattr(btd, "load_training_entity_spine", lambda s, e: spine)
    monkeypatch.setattr(bootstrap, "load_feature_store", lambda repo_path: object())
    monkeypatch.setattr(feast_retrieval, "retrieve_training_features", lambda store, sp: features)


def test_assemble_via_feast_writes_contract_columns(tmp_path, monkeypatch) -> None:
    features = pd.DataFrame([{c: 0 for c in MODEL_FEATURE_COLUMNS}])
    features["clicked"] = 1
    features["user_id"] = "u1"  # 여분 컬럼은 버려져야 한다
    _fake_env(monkeypatch, features)

    out_path = str(tmp_path / "out.csv")
    btd._assemble_via_feast(out_path, "2026-07-07", "2026-07-21")

    written = pd.read_csv(out_path)
    # 정확히 21피처 + clicked, 순서도 계약대로.
    assert list(written.columns) == [*MODEL_FEATURE_COLUMNS, "clicked"]
    assert len(written) == 1
    assert int(written["clicked"].iloc[0]) == 1


def test_assemble_via_feast_missing_feature_raises(tmp_path, monkeypatch) -> None:
    # 조회 결과에 모델 피처가 빠지면 조용히 넘기지 않고 즉시 실패.
    features = pd.DataFrame([{"category_id": "Gaming", "clicked": 1}])
    _fake_env(monkeypatch, features)
    with pytest.raises(ValueError, match="누락된 모델 피처"):
        btd._assemble_via_feast(str(tmp_path / "out.csv"), "2026-07-07", "2026-07-21")
