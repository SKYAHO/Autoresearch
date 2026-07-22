from collections.abc import Mapping, Sequence

import pytest

import src.serving.feast_reader as feast_reader
from src.serving.feast_reader import (
    FeastOnlineFeatureReader,
    load_feast_online_feature_reader,
)
from src.serving.online_features import FeatureRetrievalError


class _FakeOnlineFeatures:
    def to_dict(self) -> Mapping[str, Sequence[object]]:
        return {"user_id": ["user-1"], "age_group": ["adult"]}


class _FakeStore:
    def __init__(self) -> None:
        self.features: list[str] | None = None
        self.entity_rows: list[dict[str, str]] | None = None

    def get_online_features(
        self, *, features: list[str], entity_rows: list[dict[str, str]]
    ) -> _FakeOnlineFeatures:
        self.features = features
        self.entity_rows = entity_rows
        return _FakeOnlineFeatures()


def test_read_converts_feature_refs_and_entity_rows_for_store() -> None:
    # Given: an injected online store and immutable request collections.
    store = _FakeStore()
    reader = FeastOnlineFeatureReader(store=store)

    # When: a batched reader request is made.
    rows = reader.read(
        feature_refs=("UserStaticView:age_group",),
        entity_rows=({"user_id": "user-1"},),
    )

    # Then: the Feast-shaped call and column-oriented response are preserved.
    assert store.features == ["UserStaticView:age_group"]
    assert store.entity_rows == [{"user_id": "user-1"}]
    assert rows == {"user_id": ["user-1"], "age_group": ["adult"]}


def test_loader_prepares_ca_bundle_before_creating_store(monkeypatch) -> None:
    # Given: a factory whose bootstrapping collaborators record their order.
    events: list[str] = []
    store = _FakeStore()
    monkeypatch.setattr(
        feast_reader,
        "ensure_redis_ca_bundle",
        lambda: events.append("ca"),
    )
    monkeypatch.setattr(
        feast_reader,
        "load_feature_store",
        lambda repo_path: events.append(f"store:{repo_path}") or store,
    )

    # When: the production reader is loaded for a feature repository.
    reader = load_feast_online_feature_reader("feature_repo")

    # Then: CA preparation precedes store construction and its store is injected.
    assert events == ["ca", "store:feature_repo"]
    assert reader.store is store


def test_read_logs_error_type_and_redacts_external_store_failure(
    caplog: pytest.LogCaptureFixture,
) -> None:
    # Given: an injected external store that exposes sensitive failure details.
    class _FailingStore:
        def get_online_features(
            self, *, features: list[str], entity_rows: list[dict[str, str]]
        ) -> _FakeOnlineFeatures:
            raise RuntimeError("password=secret user_id=user-1")

    reader = FeastOnlineFeatureReader(store=_FailingStore())

    # When: the SDK-facing adapter invokes the external store.
    with caplog.at_level("ERROR", logger="src.serving.feast_reader"):
        with pytest.raises(FeatureRetrievalError) as excinfo:
            reader.read(
                feature_refs=("UserStaticView:age_group",),
                entity_rows=({"user_id": "user-1"},),
            )

    # Then: callers and operators receive only the fixed safe failure facts.
    assert str(excinfo.value) == "Feast online feature retrieval failed."
    assert len(caplog.records) == 1
    assert "error_type=RuntimeError" in caplog.text
    assert "password" not in caplog.text
    assert "secret" not in caplog.text
    assert "user-1" not in caplog.text
