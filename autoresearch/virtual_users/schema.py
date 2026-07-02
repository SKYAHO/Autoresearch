"""Virtual User 생성 파이프라인에서 공유하는 데이터 계약을 정의한다."""

from datetime import UTC, datetime
import logging
from typing import Literal

from pydantic import BaseModel, Field, field_validator


logger = logging.getLogger(__name__)

SOURCE_DATASET = "nvidia/Nemotron-Personas-Korea"
GENERATION_SCHEMA_VERSION = "virtual_user_schema_v1"
PROMPT_VERSION = "virtual_user_youtube_v1"
KR_COUNTRY = "KR"
KR_LOCALE = "ko-KR"

YOUTUBE_CATEGORIES = [
    "Gaming",
    "Music",
    "Entertainment",
    "Education",
    "News & Politics",
    "Sports",
    "Science & Technology",
    "Howto & Style",
    "People & Blogs",
    "Comedy",
]

WATCH_TIME_BANDS = ["morning", "afternoon", "evening", "night", "mixed"]


class GenerationRequest(BaseModel):
    """가상 사용자 배치 생성에 필요한 입력 조건과 출력 경로를 담는다."""

    age_min: int = 20
    age_max: int = 29
    male_count: int = 50
    female_count: int = 50
    seed: int = 42
    use_gemini: bool = True
    source_mode: Literal["huggingface", "fixture"] = "huggingface"
    output_path: str = "data/generated/virtual_users_20s_100.json"
    raw_output_path: str = "data/raw/personas/nvidia_personas_kr.jsonl"
    warehouse_output_path: str = "data/generated/virtual_users_kr.jsonl"

    @field_validator("age_min", "age_max", "male_count", "female_count")
    @classmethod
    def non_negative(cls, value: int) -> int:
        """나이와 생성 개수가 음수로 들어오는 설정 오류를 막는다."""

        if value < 0:
            raise ValueError("Generation counts and ages must be non-negative")
        return value

    @field_validator("age_max")
    @classmethod
    def valid_age_range(cls, value: int, info) -> int:
        """최대 나이가 최소 나이보다 작은 잘못된 요청을 거부한다."""

        age_min = info.data.get("age_min")
        if age_min is not None and value < age_min:
            raise ValueError("age_max must be greater than or equal to age_min")
        return value


class SourcePersona(BaseModel):
    """Hugging Face raw persona를 정규화한 내부 입력 schema."""

    uuid: str
    age: int
    sex: Literal["male", "female"]
    occupation: str = ""
    province: str = ""
    district: str = ""
    country: str = KR_COUNTRY
    locale: str = KR_LOCALE
    persona: str = ""
    hobbies_and_interests: str = ""
    hobbies_and_interests_list: list[str] = Field(default_factory=list)
    professional_persona: str = ""
    skills_and_expertise: str = ""
    sports_persona: str = ""
    arts_persona: str = ""
    travel_persona: str = ""
    culinary_persona: str = ""
    family_persona: str = ""
    cultural_background: str = ""

    @field_validator("country")
    @classmethod
    def valid_country(cls, value: str) -> str:
        if value != KR_COUNTRY:
            raise ValueError("country must be KR")
        return value

    @field_validator("locale")
    @classmethod
    def valid_locale(cls, value: str) -> str:
        if value != KR_LOCALE:
            raise ValueError("locale must be ko-KR")
        return value


class YouTubeProfile(BaseModel):
    """추천 도메인에서 사용할 YouTube 소비 성향 feature 묶음."""

    primary_categories: list[str] = Field(min_length=1, max_length=5)
    shorts_affinity: float = Field(ge=0.0, le=1.0)
    longform_affinity: float = Field(ge=0.0, le=1.0)
    trend_sensitivity: float = Field(ge=0.0, le=1.0)
    comment_propensity: float = Field(ge=0.0, le=1.0)
    watch_time_band: Literal["morning", "afternoon", "evening", "night", "mixed"]


class GenerationMeta(BaseModel):
    """생성 결과의 schema, prompt, 모델, 생성 시각을 추적하는 metadata."""

    schema_version: str
    prompt_version: str
    llm_model: str
    generated_at: str


class VirtualUser(BaseModel):
    """Data Warehouse 적재 직전의 1 user = 1 row 가상 사용자 profile."""

    virtual_user_id: str
    source_uuid: str
    source_dataset: str = SOURCE_DATASET
    country: str = "KR"
    locale: str = "ko-KR"
    age: int
    sex: Literal["male", "female"]
    age_bucket: str
    occupation: str
    province: str
    district: str = ""
    persona_summary: str
    interest_keywords: list[str] = Field(default_factory=list)
    youtube_profile: YouTubeProfile
    generation_meta: GenerationMeta

    def to_warehouse_row(self) -> dict[str, object]:
        """중첩된 profile/meta 구조를 warehouse-friendly flat row로 변환한다."""

        return {
            "user_id": self.virtual_user_id,
            "source_uuid": self.source_uuid,
            "source_dataset": self.source_dataset,
            "country": self.country,
            "locale": self.locale,
            "age": self.age,
            "sex": self.sex,
            "occupation": self.occupation,
            "province": self.province,
            "district": self.district,
            "persona_summary": self.persona_summary,
            "interest_keywords": self.interest_keywords,
            "primary_categories": self.youtube_profile.primary_categories,
            "shorts_affinity": self.youtube_profile.shorts_affinity,
            "longform_affinity": self.youtube_profile.longform_affinity,
            "trend_sensitivity": self.youtube_profile.trend_sensitivity,
            "comment_propensity": self.youtube_profile.comment_propensity,
            "watch_time_band": self.youtube_profile.watch_time_band,
            "schema_version": self.generation_meta.schema_version,
            "prompt_version": self.generation_meta.prompt_version,
            "llm_model": self.generation_meta.llm_model,
            "generated_at": self.generation_meta.generated_at,
        }


class VirtualUserBatch(BaseModel):
    """여러 명의 virtual user와 생성 요청 정보를 함께 보관하는 batch 결과."""

    schema_version: str
    prompt_version: str
    source_dataset: str
    request: GenerationRequest
    users: list[VirtualUser]
    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).replace(microsecond=0).isoformat()
    )

    @property
    def summary(self) -> dict[str, int]:
        """생성된 batch의 총원과 성별 분포를 계산한다."""

        male = sum(1 for user in self.users if user.sex == "male")
        female = sum(1 for user in self.users if user.sex == "female")
        return {
            "total": len(self.users),
            "male": male,
            "female": female,
        }

    def to_output_dict(self) -> dict[str, object]:
        """파일 저장용 dict에 batch summary를 함께 포함한다."""

        payload = self.model_dump()
        payload["summary"] = self.summary
        logger.debug(
            "Prepared virtual user batch output",
            extra={
                "summary_total": payload["summary"]["total"],
                "summary_male": payload["summary"]["male"],
                "summary_female": payload["summary"]["female"],
                "schema_version": self.schema_version,
                "prompt_version": self.prompt_version,
            },
        )
        return payload
