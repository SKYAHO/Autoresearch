from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

FeatureValue = str | int | float | bool


class CandidateVideo(BaseModel):
    model_config = ConfigDict(frozen=True)

    video_id: Annotated[str, Field(min_length=1)]
    features: dict[str, FeatureValue]


class RerankRequest(BaseModel):
    model_config = ConfigDict(frozen=True)

    user_id: Annotated[str, Field(min_length=1)]
    candidates: Annotated[list[CandidateVideo], Field(min_length=1)]


class RerankedVideo(BaseModel):
    model_config = ConfigDict(frozen=True)

    video_id: str
    ctr_score: float


class RerankResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    items: list[RerankedVideo]


class HealthcheckResponse(BaseModel):
    model_config = ConfigDict(frozen=True)

    status: str
