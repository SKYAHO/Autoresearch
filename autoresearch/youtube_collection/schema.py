"""정규화된 유튜브 트렌딩 영상 스키마(데이터 계약).

이 모듈은 수집 파이프라인 전체가 따르는 **단일 데이터 계약(single data
contract)** 을 정의한다. Kaggle 과거 데이터든, YouTube Data API v3 실시간
응답이든 모두 이 ``TrendingVideo`` 모델로 정규화된 뒤 Data Lake(GCS Parquet)에
적재된다. 즉, "원본 출처가 무엇이든 이 스키마만 맞으면 된다"는 게 핵심 설계.

왜 pydantic BaseModel 인가?
  * 타입 검증 + 자동 직렬화(model_dump)를 한 번에 얻는다.
  * load.py 가 ``model_dump()`` 로 dict 를 만들어 pyarrow Table 로 옮기고,
    transform.py 의 강제 변환(coerce) 결과가 이 모델을 통과하면 타입이 보장된다.

스키마 버전 관리
  * ``SCHEMA_VERSION`` 은 이 스키마의 식별자. 필드 구조가 바뀌면 버전을 올려
    downstream(Feature Store/Feast, 모델 학습)이 어떤 스키마를 읽고 있는지
    알 수 있게 한다. 피처 조인(point-in-time join) 시 스키마 불일치를 막는 용도.
"""
from datetime import datetime
import logging

from pydantic import BaseModel, Field


logger = logging.getLogger(__name__)

# 이 스키마의 식별자. 필드 구조 변경 시 버전 업 필요(되돌리기 비용이 큰 결정).
SCHEMA_VERSION = "youtube_trending_kr_v1"
# 이 파이프라인은 한국(KR) 트렌딩만 다룬다. transform.py 가 이 값 외의 국가는
# 거부하도록 강제한다(KR-only scope).
TARGET_COUNTRY = "KR"


class TrendingVideo(BaseModel):
    """단일 트렌딩 영상 스냅샷 1행(29개 컬럼).

    각 인스턴스 = 특정 시점(snapshot_date)의 한 영상 상태.
    같은 영상이 여러 날 트렌딩에 머물면 **매일 별도 행**으로 들어오며,
    카운트(view/like/comment)는 그날의 누적값이다. 따라서 중복 제거(dedupe) 금지 —
    일자별 델타 자체가 피처(조회수 증가율, 체류일수)의 원천이다.

    필드 그룹:
      * 영상 메타(제목/설명/태그/카테고리/길이/해상도 ...)
      * 영상 통계(view/like/comment_count) — 매일 갱신되는 누적 스냅샷
      * 채널 메타 + 통계
      * collected_at: 이 행을 수집한 시각(freshness 신호, 매일 변함)

    주의(결측 허용 필드):
      * channel_published_at: 원본 123,167행 중 8행이 null → Optional.
      * channel_subscriber_count: 채널이 구독자 수를 숨긴 경우(null).
        hidden 여부는 channel_have_hidden_subscribers 로 따로 알 수 있다.
    """

    # --- 영상 식별/메타 ---
    video_id: str
    video_published_at: datetime
    video_trending_date: datetime
    video_trending_country: str
    video_title: str
    video_description: str
    video_default_thumbnail: str
    # 카테고리 '이름'(예: "Music", "Sports"). API는 숫자 id 를 주지만
    # videoCategories.list 로 이름으로 변환해서 저장한다. Kaggle 원본도 이름 문자열.
    video_category: str
    video_tags: list[str]
    video_duration: str  # ISO 8601 (예: "PT10M15S")
    video_dimension: str  # "2d" / "3d"
    video_definition: str  # "hd" / "sd"
    video_licensed_content: bool

    # --- 영상 통계(누적 스냅샷, Field(ge=0) 로 음수 방지) ---
    video_view_count: int = Field(ge=0)
    video_like_count: int = Field(ge=0)
    video_comment_count: int = Field(ge=0)

    # --- 채널 메타 ---
    channel_id: str
    channel_title: str
    channel_description: str
    channel_custom_url: str
    channel_published_at: datetime | None = None  # 원본 일부 결측 → Optional
    channel_country: str

    # --- 채널 통계 ---
    channel_view_count: int = Field(ge=0)
    channel_subscriber_count: int | None = Field(default=None, ge=0)  # 숨김 시 null
    channel_have_hidden_subscribers: bool
    channel_video_count: int = Field(ge=0)
    channel_localized_title: str
    channel_localized_description: str

    # --- 수집 메타 ---
    # 이 행을 수집한 시각. freshness 신호. snapshot_date 파티션 키의 원천.
    collected_at: datetime
