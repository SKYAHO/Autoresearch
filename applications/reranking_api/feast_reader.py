from __future__ import annotations

__arch__ = {
    "stage": "training",
    "role": "Feast FeatureStore를 서빙의 열 지향 온라인 조회 계약에 맞추는 어댑터입니다.",
    "owns": [
        "Feast get_online_features 호출과 결과 변환",
        "Redis CA·FeatureStore bootstrap",
        "외부 SDK 오류의 안전한 진단과 조회 오류 변환",
    ],
    "not_owns": [
        "피처 계약 검증과 cold-start 기본값",
        "HTTP 오류 매핑",
        "Feast 피처 정의와 materialization",
    ],
}

import logging
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Protocol

from feature_repo.bootstrap import ensure_redis_ca_bundle, load_feature_store
from src.serving.online_features import (
    FeatureRetrievalError,
    FeatureRows,
)

logger = logging.getLogger(__name__)


class _OnlineFeatures(Protocol):
    def to_dict(self) -> FeatureRows:
        ...


class _FeatureStore(Protocol):
    def get_online_features(
        self, *, features: list[str], entity_rows: list[dict[str, str]]
    ) -> _OnlineFeatures:
        ...


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
        except Exception as error:  # noqa: BLE001 - external SDK errors may contain request data.
            error_type = type(error).__name__
            logger.error(
                "Feast online feature retrieval failed. error_type=%s",
                error_type,
                extra={"error_type": error_type},
            )
            raise FeatureRetrievalError(
                reason="Feast online feature retrieval failed."
            ) from None


def load_feast_online_feature_reader(
    repo_path: str | Path,
) -> FeastOnlineFeatureReader:
    """Redis CA를 준비한 뒤 Feast online reader를 생성한다."""
    ensure_redis_ca_bundle()
    return FeastOnlineFeatureReader(store=load_feature_store(repo_path))
