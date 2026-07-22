import sys
from collections.abc import Mapping, Sequence

import pytest

import feature_repo.bootstrap as bootstrap

pytest.importorskip("feast")

from feast.infra.online_stores.redis import RedisOnlineStore
from feast.online_response import OnlineResponse
from feast.protos.feast.serving.ServingService_pb2 import GetOnlineFeaturesResponse
from feast.protos.feast.types.Value_pb2 import Value


class _FakeOnlineFeatures:
    def to_dict(self) -> Mapping[str, Sequence[object]]:
        return {"user_id": ["user-1"], "age_group": ["adult"]}


def test_redis_missing_entities_keep_one_null_value_row_per_entity() -> None:
    rows = RedisOnlineStore()._convert_redis_values_to_protobuf(
        redis_values=[[None, None], [None, None]],
        feature_view="VideoFeatureView",
        requested_features=["category_id", "_ts:VideoFeatureView"],
    )

    assert len(rows) == 2
    assert [values["category_id"].WhichOneof("val") for _, values in rows] == [None, None]


def test_feast_online_response_returns_native_numeric_scalars() -> None:
    response = GetOnlineFeaturesResponse()
    response.metadata.feature_names.val.extend(["count", "ratio", "missing"])
    for value in (Value(int64_val=4), Value(float_val=0.25), Value()):
        response.results.add().values.append(value)

    rows = OnlineResponse(response).to_dict()

    assert rows == {"count": [4], "ratio": [0.25], "missing": [None]}
    assert type(rows["count"][0]) is int
    assert type(rows["ratio"][0]) is float


def test_load_feature_store_resolves_external_repo_and_prepares_import_path(
    monkeypatch, tmp_path
) -> None:
    # Given: an external feature repository with a custom online-store module.
    import feast

    repo_path = tmp_path / "external" / "feature_repo"
    repo_path.mkdir(parents=True)
    captured: list[str] = []
    store = object()
    monkeypatch.setattr(
        feast,
        "FeatureStore",
        lambda *, repo_path: captured.append(repo_path) or store,
    )

    # When: the shared bootstrap constructs its FeatureStore.
    loaded = bootstrap.load_feature_store(repo_path)

    # Then: Feast receives the absolute repo path and can import its package.
    assert loaded is store
    assert captured == [str(repo_path.resolve())]
    assert sys.path[0] == str(repo_path.parent.resolve())
