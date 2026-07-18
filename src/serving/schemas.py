from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

FeatureValue = str | int | float | bool


class CandidateVideo(BaseModel):
    """재정렬 대상 후보 영상 하나. video_id와 모델 입력 피처 맵을 담는다."""

    model_config = ConfigDict(frozen=True)

    video_id: Annotated[str, Field(min_length=1)]
    features: dict[str, FeatureValue]


class RerankRequest(BaseModel):
    """/rerank 요청 본문. 요청 유저와 최소 1개 이상의 후보 목록."""

    model_config = ConfigDict(frozen=True)

    user_id: Annotated[str, Field(min_length=1)]
    candidates: Annotated[list[CandidateVideo], Field(min_length=1)]


class RerankedVideo(BaseModel):
    """재정렬 결과 항목 하나. 영상 ID와 예측된 CTR 점수."""

    model_config = ConfigDict(frozen=True)

    video_id: str
    ctr_score: float


class RerankResponse(BaseModel):
    """/rerank 응답 본문. CTR 점수 내림차순으로 정렬된 결과 목록."""

    model_config = ConfigDict(frozen=True)

    items: list[RerankedVideo]


class HealthcheckResponse(BaseModel):
    """/healthcheck 응답 본문. 서비스 상태 문자열."""

    model_config = ConfigDict(frozen=True)

    status: str
