from __future__ import annotations

from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

FeatureValue = str | int | float | bool


# BASE MODEL 은 pydantic(파이썬의 타입을 자동검증 해주는 친구) 라이브러리가 제공하는 클래스
# 역할은 들어오는 데이터의 유효성을 검증하고, 데이터 구조를 정의하는 것이다.

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

    model_config = ConfigDict(frozen=True) #설정 dict 의 구성을 불변요소로 만들어라 -> model config 의 스키마는 

    items: list[RerankedVideo]             # 데이터필드가 이렇게 되어있다는 뜼임 


class HealthcheckResponse(BaseModel):
    """/healthcheck 응답 본문. 서비스 상태 문자열."""

    model_config = ConfigDict(frozen=True)

    status: str
