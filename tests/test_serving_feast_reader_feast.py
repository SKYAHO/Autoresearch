import sys
from collections.abc import Mapping, Sequence

import pytest

import feature_repo.bootstrap as bootstrap
from src.serving.feast_reader import FeastOnlineFeatureReader
from src.serving.online_features import FeatureRetrievalError

pytest.importorskip("feast")


class _FakeOnlineFeatures:
    def to_dict(self) -> Mapping[str, Sequence[object]]:
        return {"user_id": ["user-1"], "age_group": ["adult"]}


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


def test_read_converts_feast_error_without_request_values() -> None:
    # Given: a Feast-only failing store whose error text contains sensitive input.
    from feast.errors import FeastError

    class _FailingStore:
        def get_online_features(
            self, *, features: list[str], entity_rows: list[dict[str, str]]
        ) -> _FakeOnlineFeatures:
            raise FeastError("password=secret user_id=user-1")

    reader = FeastOnlineFeatureReader(store=_FailingStore())

    # When: the adapter reads online features.
    with pytest.raises(FeatureRetrievalError) as excinfo:
        reader.read(
            feature_refs=("UserStaticView:age_group",),
            entity_rows=({"user_id": "user-1"},),
        )

    # Then: the client-facing error is stable and contains no request or secret.
    assert str(excinfo.value) == "Feast online feature retrieval failed."
