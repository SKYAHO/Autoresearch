"""ODFV(category_match_view) 조회 스모크 (#358, [EPIC] #299 Phase 2).

파생 2종(preferred_category_match / historical_category_match)을 On-Demand
FeatureView로 정의한 뒤, 실제 ``get_historical_features``로 조회해 **세 소스 뷰
(UserStatic/UserDynamic/Video)의 컬럼이 ODFV 변환에 올바르게 먹여지는지**를 검증한다.
BigQuery 없이 로컬 File 오프라인 스토어로 돈다.

drift 방지: 프로덕션 뷰 스키마(`feature_definitions`)와 ODFV 변환 함수
(`compute_category_matches`)를 그대로 재사용하고 소스만 File로 바꾼다.
"""

import os

import pytest

pytest.importorskip("feast")

import pandas as pd  # noqa: E402
from feast import (  # noqa: E402
    FeatureStore,
    FeatureView,
    Field,
    FileSource,
    RepoConfig,
)
from feast.on_demand_feature_view import on_demand_feature_view  # noqa: E402
from feast.repo_config import RegistryConfig  # noqa: E402
from feast.types import Int64  # noqa: E402

os.environ.setdefault("GCP_PROJECT_ID", "smoke")
os.environ.setdefault("BQ_DATASET", "smoke")

from feature_repo.feature_definitions import (  # noqa: E402
    compute_category_matches,
    ctr_training_service,
    user_dynamic_view,
    user_entity,
    user_static_view,
    video_entity,
    video_feature_view,
)
from src.features.model_contract import MODEL_FEATURE_COLUMNS  # noqa: E402

_UTC = "UTC"
_TS = pd.Timestamp("2026-07-01", tz=_UTC)


def _default(dtype) -> object:
    s = str(dtype)
    if "Array" in s:
        return []
    if "Int" in s:
        return 0
    if "Float" in s:
        return 0.0
    return ""


def _row(view: FeatureView, keys: dict, **override) -> dict:
    """프로덕션 뷰 스키마의 모든 Field를 기본값으로 채우고 일부만 덮어쓴다."""
    row = {**keys, "event_timestamp": _TS}
    for f in view.schema:
        row[f.name] = override.get(f.name, _default(f.dtype))
    return row


def _file_view(src_view: FeatureView, path: str, rows: list[dict], entities: list) -> FeatureView:
    # src_view.entities는 Entity 객체가 아니라 이름(문자열)이므로 객체를 명시적으로 받는다.
    pd.DataFrame(rows).to_parquet(path, index=False)
    return FeatureView(
        name=src_view.name + "Smoke",
        entities=entities,
        schema=[Field(name=f.name, dtype=f.dtype) for f in src_view.schema],
        source=FileSource(path=path, timestamp_field="event_timestamp"),
        online=False,
        ttl=src_view.ttl,
    )


def _build_store() -> FeatureStore:
    static = _file_view(
        user_static_view, "static.parquet",
        [_row(user_static_view, {"user_id": "u1"}, preferred_category=["Gaming", "Music"])],
        [user_entity],
    )
    dynamic = _file_view(
        user_dynamic_view, "dynamic.parquet",
        [_row(user_dynamic_view, {"user_id": "u1"}, historical_category_affinity="Gaming")],
        [user_entity],
    )
    video = _file_view(
        video_feature_view, "video.parquet",
        [
            _row(video_feature_view, {"video_id": "v1"}, category_id="Gaming"),
            _row(video_feature_view, {"video_id": "v2"}, category_id="Sports"),
        ],
        [video_entity],
    )

    @on_demand_feature_view(
        sources=[static, dynamic, video],
        schema=[
            Field(name="preferred_category_match", dtype=Int64),
            Field(name="historical_category_match", dtype=Int64),
        ],
    )
    def match_smoke(inputs: pd.DataFrame) -> pd.DataFrame:
        return compute_category_matches(inputs)

    store = FeatureStore(
        config=RepoConfig(
            project="smoke",
            provider="local",
            registry=RegistryConfig(path="registry.db"),
            offline_store={"type": "file"},
            entity_key_serialization_version=3,
        )
    )
    store.apply([user_entity, video_entity, static, dynamic, video, match_smoke])
    return store


def test_feature_service_matches_model_contract() -> None:
    # ctr_training_v1이 정확히 MODEL_FEATURE_COLUMNS 21개로 해소되는지(계약 대조).
    names = [f.name for proj in ctr_training_service.feature_view_projections for f in proj.features]
    assert len(names) == len(MODEL_FEATURE_COLUMNS) == 21
    assert set(names) == set(MODEL_FEATURE_COLUMNS)


def test_odfv_computes_matches_from_three_source_views(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = _build_store()
    # u1: preferred=[Gaming,Music], hist=Gaming. v1=Gaming, v2=Sports. 조회 07-02(ttl 안).
    entity_df = pd.DataFrame(
        [
            {"user_id": "u1", "video_id": "v1", "event_timestamp": pd.Timestamp("2026-07-02", tz=_UTC)},
            {"user_id": "u1", "video_id": "v2", "event_timestamp": pd.Timestamp("2026-07-02", tz=_UTC)},
        ]
    )
    out = store.get_historical_features(
        entity_df=entity_df,
        features=[
            "match_smoke:preferred_category_match",
            "match_smoke:historical_category_match",
        ],
    ).to_df()
    by_video = out.set_index("video_id")
    # v1=Gaming: preferred(Gaming∈[Gaming,Music])=1, historical(Gaming==Gaming)=1
    assert int(by_video.loc["v1", "preferred_category_match"]) == 1
    assert int(by_video.loc["v1", "historical_category_match"]) == 1
    # v2=Sports: preferred(Sports∉[..])=0, historical(Gaming!=Sports)=0
    assert int(by_video.loc["v2", "preferred_category_match"]) == 0
    assert int(by_video.loc["v2", "historical_category_match"]) == 0
