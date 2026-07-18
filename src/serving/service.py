from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from src.serving.schemas import CandidateVideo, FeatureValue, RerankedVideo


@runtime_checkable
class ProbabilityModel(Protocol):
    """predict_proba를 제공하는 모델의 구조적 계약(LightGBM 등 sklearn 호환 분류기)."""

    def predict_proba(self, features: pd.DataFrame) -> np.ndarray: ...

## Frozen , slots 내가 만든 클래스에서 적합한 설정을 골라야 한다 

@dataclass(frozen=True, slots=True) 
class MissingFeatureColumnsError(Exception):
    """요청 후보들에 모델이 요구하는 피처 컬럼이 빠졌을 때 발생한다(→ HTTP 422)."""

    columns: tuple[str, ...]

    def __str__(self) -> str:
        return f"Missing required model features: {', '.join(self.columns)}"


@dataclass(frozen=True, slots=True)
class PredictionError(Exception):
    """모델 예측이 실패하거나 결과 형태가 계약과 다를 때 발생한다(→ HTTP 500)."""

    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class RerankOutcome:
    """rerank 결과와 서빙 계측용 진단 정보를 함께 담는다.

    unseen_categories: 학습 카테고리에 없어 NaN으로 조용히 강등된 값들을 컬럼별로 모은 것.
    값이 present였는데 강등된 경우만 담기며, 상위 계층(app)이 이를 카운터·로깅으로 계측한다.
    """

    items: list[RerankedVideo]
    unseen_categories: Mapping[str, tuple[FeatureValue, ...]]


@dataclass(frozen=True, slots=True)
class Reranker:
    """학습된 모델·피처 계약을 담고, 후보 영상을 CTR 확률로 재정렬하는 핵심 도메인 객체."""

    model: ProbabilityModel
    feature_columns: tuple[str, ...]
    categorical_categories: Mapping[str, tuple[FeatureValue, ...]]

    def rerank(self, candidates: Sequence[CandidateVideo]) -> list[RerankedVideo]:
        """후보 피처를 검증·정규화해 CTR 확률을 예측하고, 점수 내림차순으로 정렬해 반환한다."""
        return self.rerank_with_diagnostics(candidates).items

    def rerank_with_diagnostics(
        self, candidates: Sequence[CandidateVideo]
    ) -> RerankOutcome:
        """rerank과 동일하게 예측·정렬하되, 학습에 없어 NaN으로 강등된 카테고리 값을 함께 보고한다."""
        if not candidates:
            return RerankOutcome(items=[], unseen_categories={})

        common_keys = set(candidates[0].features)
        for candidate in candidates[1:]:
            common_keys &= candidate.features.keys()
        missing_columns = tuple(
            column for column in self.feature_columns if column not in common_keys
        )
        if missing_columns:
            raise MissingFeatureColumnsError(columns=missing_columns)

        feature_frame = pd.DataFrame(
            [candidate.features for candidate in candidates], columns=self.feature_columns
        )
        # 학습 시점 카테고리·순서를 그대로 재현해야 LightGBM category 코드가 일치한다.
        # 학습에 없던 값은 NaN(결측)으로 처리된다 — 이 강등은 예외 없이 일어나므로,
        # present였다가 NaN이 된 값만 골라 진단으로 보고해 조용한 degradation을 계측 가능하게 한다.
        unseen_categories: dict[str, tuple[FeatureValue, ...]] = {}
        for column, categories in self.categorical_categories.items():
            original = feature_frame[column]
            converted = pd.Categorical(original, categories=categories)
            coerced = original.notna().to_numpy() & np.asarray(pd.isna(converted))
            if coerced.any():
                unseen_categories[column] = tuple(original.to_numpy()[coerced].tolist())
            feature_frame[column] = converted

        try:
            probabilities = self.model.predict_proba(feature_frame)
            if probabilities.ndim != 2 or probabilities.shape != (len(candidates), 2):
                raise PredictionError(reason="Model returned an invalid probability matrix.")
        except PredictionError:
            raise
        except Exception as error:
            raise PredictionError(reason="Model prediction raised an exception.") from error

        ranked_items = [
            RerankedVideo(video_id=candidate.video_id, ctr_score=float(probability[1]))
            for candidate, probability in zip(candidates, probabilities, strict=True)
        ]
        ranked_items.sort(key=lambda item: item.ctr_score, reverse=True)
        return RerankOutcome(items=ranked_items, unseen_categories=unseen_categories)
