"""offline get_historical_features PIT 조회 스모크 (#356, [EPIC] #299 Phase 0).

실제 offline_store는 BigQuery라 CI에서 조회할 수 없다. 대신 **로컬 파일 오프라인
스토어**로 ``get_historical_features``를 실제 실행해, PIT(as-of) 의미가 우리
FeatureView 스키마에서 동작함을 검증한다.

스키마 drift 방지: 프로덕션 ``UserDynamicView``의 ``schema``(Field 목록)와
``user_entity``를 그대로 재사용하고 소스만 ``FileSource``로 바꾼다. BQ/File 두
정의가 손으로 어긋나면 스모크가 실물과 다른 걸 검증하게 되므로, 컬럼은 단일
소스(``feature_definitions.py``)에서 가져온다.

이 테스트는 동시에 **ttl 부재 → 결손일 stale fallback**을 시연한다: 07-02 스냅샷이
결손인 상태로 07-02 시점을 조회하면, ttl이 없으므로 null이 아니라 **더 오래된
07-01 스냅샷**이 조용히 붙는다.
"""

import os

import pytest

pytest.importorskip("feast")

import pandas as pd  # noqa: E402
from feast import FeatureStore, FeatureView, Field, FileSource, RepoConfig  # noqa: E402
from feast.repo_config import RegistryConfig  # noqa: E402

# feature_definitions는 import 시 BigQuerySource 구성을 위해 이 두 env를 요구한다.
# 로컬 스모크는 File 소스로 대체하므로 더미 값으로 import만 성립시킨다.
os.environ.setdefault("GCP_PROJECT_ID", "smoke")
os.environ.setdefault("BQ_DATASET", "smoke")

from feature_repo.feature_definitions import user_dynamic_view, user_entity  # noqa: E402

_UTC = "UTC"


def _snapshot(user_id: str, day: str, clicks: int) -> dict:
    """UserDynamicView 스키마에 맞는 한 스냅샷 행."""
    return {
        "user_id": user_id,
        "event_timestamp": pd.Timestamp(day, tz=_UTC),
        "recent_click_count_7d": clicks,
        "recent_view_count_7d": 0,
        "recent_watch_time_7d": 0,
        "recent_like_count_7d": 0,
        "historical_category_affinity": "unknown",
        "total_event_count_7d": clicks,
    }


def _build_store() -> FeatureStore:
    # 경로는 cwd(테스트에서 tmp_path로 chdir) 기준 상대경로를 쓴다. feast의 file
    # registry가 Windows 절대경로(드라이브레터를 URI scheme로 오인)를 못 다루기
    # 때문 - 상대경로는 Windows/Linux CI 양쪽에서 안전하다.
    data_path = "user_dynamic.parquet"
    # u1: 07-01, 07-03에 스냅샷. 07-02은 결손(gap).
    pd.DataFrame(
        [
            _snapshot("u1", "2026-07-01", clicks=11),
            _snapshot("u1", "2026-07-03", clicks=33),
        ]
    ).to_parquet(data_path, index=False)

    source = FileSource(path=data_path, timestamp_field="event_timestamp")
    # 프로덕션 스키마를 그대로 재사용(이름·타입 단일 소스). ttl=None도 프로덕션과 동일.
    smoke_view = FeatureView(
        name="UserDynamicSmoke",
        entities=[user_entity],
        schema=[Field(name=f.name, dtype=f.dtype) for f in user_dynamic_view.schema],
        source=source,
        online=False,
        ttl=None,
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
    store.apply([user_entity, smoke_view])
    return store


def _click_at(store: FeatureStore, ts: str) -> int:
    out = store.get_historical_features(
        entity_df=pd.DataFrame([{"user_id": "u1", "event_timestamp": pd.Timestamp(ts, tz=_UTC)}]),
        features=["UserDynamicSmoke:recent_click_count_7d"],
    ).to_df()
    return int(out["recent_click_count_7d"].iloc[0])


def test_get_historical_features_selects_as_of_snapshot(tmp_path, monkeypatch) -> None:
    # 07-01 스냅샷 이후·07-03 이전 시점 → 이전 스냅샷(07-01)을 고르고 미래(07-03)는 안 붙는다.
    monkeypatch.chdir(tmp_path)
    store = _build_store()
    assert _click_at(store, "2026-07-02 12:00") == 11
    # 07-03 스냅샷 이후 시점 → 07-03을 고른다(PIT가 최신 as-of를 선택).
    assert _click_at(store, "2026-07-04 12:00") == 33


def test_missing_day_falls_back_to_stale_snapshot_without_ttl(tmp_path, monkeypatch) -> None:
    # ttl 부재 실증: 07-02 스냅샷이 결손인데 07-02를 조회하면 null이 아니라 더
    # 오래된 07-01 스냅샷(stale)이 붙는다. #357에서 ttl 도입으로 막을 대상.
    monkeypatch.chdir(tmp_path)
    store = _build_store()
    assert _click_at(store, "2026-07-02 23:59") == 11
