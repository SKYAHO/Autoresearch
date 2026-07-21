from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from feature_repo.bootstrap import ensure_redis_ca_bundle, load_feature_store
from src.serving.online_features import (
    FeatureRetrievalError,
    FeatureRows,
)


class _OnlineFeatures(Protocol):
    def to_dict(self) -> FeatureRows:
        ...


class _FeatureStore(Protocol):
    def get_online_features(
        self, *, features: list[str], entity_rows: list[dict[str, str]]
    ) -> _OnlineFeatures:
        ...


def _feast_error_type() -> type[Exception]:
    from feast.errors import FeastError

    return FeastError


@dataclass(frozen=True, slots=True)
class FeastOnlineFeatureReader:
    """주입된 Feast store로 online feature 요청을 수행한다."""

    store: _FeatureStore

    def read(
        self,
        *,
        feature_refs: Sequence[str],
        entity_rows: Sequence[Mapping[str, str]],
    ) -> FeatureRows:
        """Feast 결과를 기존 열 지향 reader 계약으로 반환한다."""
        try:
            return self.store.get_online_features(
                features=list(feature_refs),
                entity_rows=[dict(row) for row in entity_rows],
            ).to_dict()
        except _feast_error_type():
            raise FeatureRetrievalError(
                reason="Feast online feature retrieval failed."
            ) from None


def load_feast_online_feature_reader(
    repo_path: str | Path,
) -> FeastOnlineFeatureReader:
    """Redis CA를 준비한 뒤 Feast online reader를 생성한다."""
    ensure_redis_ca_bundle()
    return FeastOnlineFeatureReader(store=load_feature_store(repo_path))
