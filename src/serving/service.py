from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from typing import Protocol, runtime_checkable

import numpy as np
import pandas as pd

from src.serving.schemas import CandidateVideo, FeatureValue, RerankedVideo


@runtime_checkable
class ProbabilityModel(Protocol):
    def predict_proba(self, features: pd.DataFrame) -> np.ndarray: ...


@dataclass(frozen=True, slots=True)
class MissingFeatureColumnsError(Exception):
    columns: tuple[str, ...]

    def __str__(self) -> str:
        return f"Missing required model features: {', '.join(self.columns)}"


@dataclass(frozen=True, slots=True)
class PredictionError(Exception):
    reason: str

    def __str__(self) -> str:
        return self.reason


@dataclass(frozen=True, slots=True)
class Reranker:
    model: ProbabilityModel
    feature_columns: tuple[str, ...]
    categorical_categories: Mapping[str, tuple[FeatureValue, ...]]

    def rerank(self, candidates: Sequence[CandidateVideo]) -> list[RerankedVideo]:
        missing_columns = tuple(
            column
            for column in self.feature_columns
            if any(column not in candidate.features for candidate in candidates)
        )
        if missing_columns:
            raise MissingFeatureColumnsError(columns=missing_columns)

        feature_frame = pd.DataFrame(
            [candidate.features for candidate in candidates], columns=self.feature_columns
        )
        # 학습 시점 카테고리·순서를 그대로 재현해야 LightGBM category 코드가 일치한다.
        # 학습에 없던 값은 NaN(결측)으로 처리된다.
        for column, categories in self.categorical_categories.items():
            feature_frame[column] = pd.Categorical(
                feature_frame[column], categories=categories
            )

        probabilities = self.model.predict_proba(feature_frame)
        if probabilities.ndim != 2 or probabilities.shape != (len(candidates), 2):
            raise PredictionError(reason="Model returned an invalid probability matrix.")

        ranked_items = [
            RerankedVideo(video_id=candidate.video_id, ctr_score=float(probability[1]))
            for candidate, probability in zip(candidates, probabilities, strict=True)
        ]
        return sorted(ranked_items, key=lambda item: item.ctr_score, reverse=True)
