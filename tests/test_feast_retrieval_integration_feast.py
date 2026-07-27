"""staged Feast 조회 통합 스모크 (#358, [EPIC] #299 Phase 2).

``feast_retrieval.retrieve_training_features``의 2단 조회(video PIT로 category 확정 →
전체 FeatureService 조회)가 spine에 **21피처를 모두** 붙이는지 로컬 File 스토어로
검증한다. 특히 (a) topic_similarity가 staged category_id로 조회되는지, (b) ODFV 파생
2종이 계산되는지, (c) spine의 passthrough(clicked)가 보존되는지를 본다.

drift 방지: 프로덕션 뷰 스키마·이름·ODFV 변환·FeatureService 구성을
(``feature_definitions``) 그대로 재현하고 소스만 File로 바꾼다 — 헬퍼의 하드코딩된
이름(VideoFeatureView / ctr_training_v1)이 그대로 맞물린다.
"""

import os

import pytest

pytest.importorskip("feast")

import pandas as pd
from feast import (
    FeatureService,
    FeatureStore,
    FeatureView,
    Field,
    FileSource,
    RepoConfig,
)
from feast.on_demand_feature_view import on_demand_feature_view
from feast.repo_config import RegistryConfig
from feast.types import Int64

os.environ.setdefault("GCP_PROJECT_ID", "smoke")
os.environ.setdefault("BQ_DATASET", "smoke")

from feature_repo.feature_definitions import (
    category_entity,
    compute_category_matches,
    user_category_similarity_view,
    user_dynamic_view,
    user_entity,
    user_static_view,
    video_entity,
    video_feature_view,
)
from src.features.feast_retrieval import (
    build_pool_feature_frame_feast,
    retrieve_training_features,
)
from src.features.model_contract import MODEL_FEATURE_COLUMNS

_UTC = "UTC"
_TS = pd.Timestamp("2026-07-01", tz=_UTC)
_QUERY_TS = pd.Timestamp("2026-07-02", tz=_UTC)


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
    row = {**keys, "event_timestamp": _TS}
    for f in view.schema:
        row[f.name] = override.get(f.name, _default(f.dtype))
    return row


def _file_view(
    src_view: FeatureView, name: str, path: str, rows: list[dict], entities: list,
    field_mapping: dict | None = None,
) -> FeatureView:
    # 프로덕션 이름을 그대로 써서 헬퍼의 하드코딩 참조(VideoFeatureView 등)와 맞춘다.
    pd.DataFrame(rows).to_parquet(path, index=False)
    return FeatureView(
        name=name,
        entities=entities,
        schema=[Field(name=f.name, dtype=f.dtype) for f in src_view.schema],
        source=FileSource(
            path=path, timestamp_field="event_timestamp", field_mapping=field_mapping or {}
        ),
        online=False,
        ttl=src_view.ttl,
    )


def _build_store() -> FeatureStore:
    static = _file_view(
        user_static_view, "UserStaticView", "static.parquet",
        [_row(user_static_view, {"user_id": "u1"}, preferred_category=["Gaming"],
              age_group="20s", occupation="student", watch_time_band="night")],
        [user_entity],
    )
    dynamic = _file_view(
        user_dynamic_view, "UserDynamicView", "dynamic.parquet",
        [_row(user_dynamic_view, {"user_id": "u1"}, historical_category_affinity="Gaming",
              recent_click_count_7d=5, total_event_count_7d=9)],
        [user_entity],
    )
    video = _file_view(
        video_feature_view, "VideoFeatureView", "video.parquet",
        [_row(video_feature_view, {"video_id": "v1"}, category_id="Gaming",
              duration_sec=300, view_count=1000)],
        [video_entity],
    )
    # 물리 컬럼은 category_id, Feast 조인키는 category_key(field_mapping) — 프로덕션과 동일.
    similarity = _file_view(
        user_category_similarity_view, "UserCategorySimilarityView", "sim.parquet",
        [_row(user_category_similarity_view, {"user_id": "u1", "category_id": "Gaming"},
              topic_similarity=0.9)],
        [user_entity, category_entity],
        field_mapping={"category_id": "category_key"},
    )

    @on_demand_feature_view(
        sources=[static, dynamic, video],
        schema=[
            Field(name="preferred_category_match", dtype=Int64),
            Field(name="historical_category_match", dtype=Int64),
        ],
    )
    def category_match_view(inputs: pd.DataFrame) -> pd.DataFrame:
        return compute_category_matches(inputs)

    service = FeatureService(
        name="ctr_training_v1",
        features=[
            static[["age_group", "occupation", "watch_time_band"]],
            dynamic,
            video,
            similarity[["topic_similarity"]],
            category_match_view,
        ],
    )
    store = FeatureStore(
        config=RepoConfig(
            project="smoke",
            provider="local",
            registry=RegistryConfig(path="registry.db"),
            offline_store={"type": "file"},
            entity_key_serialization_version=3,
        )
    )
    store.apply(
        [user_entity, video_entity, category_entity, static, dynamic, video,
         similarity, category_match_view, service]
    )
    return store


def test_staged_retrieval_attaches_all_21_features(tmp_path, monkeypatch) -> None:
    monkeypatch.chdir(tmp_path)
    store = _build_store()
    spine = pd.DataFrame(
        [{"user_id": "u1", "video_id": "v1", "event_timestamp": _QUERY_TS, "clicked": 1}]
    )
    out = retrieve_training_features(store, spine)

    assert len(out) == 1
    row = out.iloc[0]
    # 21피처가 전부 결과에 존재.
    assert set(MODEL_FEATURE_COLUMNS).issubset(out.columns)
    # staged 조회: topic_similarity가 category_id=Gaming으로 조회됨.
    assert row["topic_similarity"] == pytest.approx(0.9)
    # ODFV 파생: Gaming 매칭 → 1/1.
    assert int(row["preferred_category_match"]) == 1
    assert int(row["historical_category_match"]) == 1
    # 소스 값 확인 + spine passthrough(clicked).
    assert row["category_id"] == "Gaming"
    assert int(row["recent_click_count_7d"]) == 5
    assert int(row["clicked"]) == 1


def test_build_pool_feature_frame_feast_end_to_end(tmp_path, monkeypatch) -> None:
    """서빙 pool 조립(build_pool_feature_frame_feast)이 실물 Feast API로 staged 조회를
    물리적으로 맞물려 돌리는지 검증(#359 A5) — fake-store 유닛이 못 잡는 간극.

    반환 계약(video_id + 21피처)과 값(staged category 조회·ODFV 파생)을 present 후보로 본다.

    NOTE: "미발견 영상 no-drop + cold-start"(설계결정 3)는 **BigQuery 전용** 계약이다 —
    File store는 미발견 엔티티가 섞인 다중 뷰 PIT 조회에서 행을 드롭한다(실측,
    retrieve_training_features NOTE와 동일). 그래서 여기선 present 후보만 넣어 배관을
    검증하고, 결손 행 보존+cold-start는 Phase B의 BigQuery 실측에서 확인한다. drop 없이
    cold-start만 적용하는 **로직**은 test_build_pool_feature_frame_feast.py의 fake-store
    유닛이 가드한다.
    """
    monkeypatch.chdir(tmp_path)
    store = _build_store()
    out = build_pool_feature_frame_feast(store, "u1", ["v1"], "2026-07-02 00:00:00")

    # 반환 계약: video_id + 21피처, present 후보 1행.
    assert list(out.columns) == ["video_id", *MODEL_FEATURE_COLUMNS]
    assert out["video_id"].tolist() == ["v1"]
    row = out.iloc[0]
    # staged 조회(category=Gaming)·소스 값·ODFV 파생이 실물 API로 붙는다.
    assert row["category_id"] == "Gaming"
    assert int(row["recent_click_count_7d"]) == 5
    assert row["topic_similarity"] == pytest.approx(0.9)
    assert int(row["preferred_category_match"]) == 1
    assert int(row["historical_category_match"]) == 1
