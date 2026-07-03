"""GLM 또는 fixture rule로 SourcePersona를 VirtualUser로 변환한다."""

import json
import logging
import os
from datetime import UTC, datetime

from autoresearch.virtual_users.categories import (
    DEFAULT_KAGGLE_YOUTUBE_CATEGORIES,
    build_category_affinity,
)
from autoresearch.virtual_users.interests import extract_virtual_user_interests
from autoresearch.virtual_users.schema import (
    DerivedVirtualUserFeatures,
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_COUNTRY,
    SOURCE_LOCALE,
    SourcePersona,
    VirtualUser,
    age_bucket_for_age,
)


logger = logging.getLogger(__name__)

DEFAULT_GLM_MODEL = "glm-5.2"
DEFAULT_ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
GLM_SYSTEM_HARNESS = """너는 virtual user feature extractor다.
source data의 demographic/factual 필드는 절대 변경하지 마라.
모든 persona 컬럼을 근거로 관심사와 취향을 추론하라.
출력은 지정된 derived JSON schema만 허용한다.
없는 정보를 만들지 말고 source에서 추론 가능한 수준만 생성하라.
category는 제공된 allowed category vocabulary 안에서만 선택하라.
generation_meta는 만들지 마라.
"""


def build_virtual_user_prompt(persona: SourcePersona, virtual_user_id: str) -> str:
    """SourcePersona 전체 payload와 허용 category vocab을 GLM user prompt로 만든다."""

    persona_payload = persona.model_dump()
    allowed_categories = "\n".join(
        f"- {category}" for category in DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
    )
    prompt = f"""You convert a Korean synthetic persona into derived virtual YouTube user features.

Prompt version: {PROMPT_VERSION}
Schema version: {GENERATION_SCHEMA_VERSION}
Virtual user id: {virtual_user_id}

Source persona:
{json.dumps(persona_payload, ensure_ascii=False, indent=2)}

Allowed category vocabulary:
{allowed_categories}

Return only JSON. Do not include Markdown. Do not include commentary.

Required derived JSON shape:
{{
  "persona_summary": "one Korean or English sentence",
  "hobby_keywords": ["keyword inferred from source hobbies"],
  "interest_keywords": ["keyword inferred from source persona text"],
  "lifestyle_keywords": ["daily life keyword inferred from source"],
  "food_keywords": ["food keyword inferred from source"],
  "travel_keywords": ["travel keyword inferred from source"],
  "career_keywords": ["career keyword inferred from source"],
  "family_context_keywords": ["family or household keyword inferred from source"],
  "primary_categories": ["Gaming", "Music"],
  "category_evidence": {{
    "Gaming": ["short source-grounded phrase"]
  }},
  "shorts_affinity": 0.0,
  "longform_affinity": 0.0,
  "trend_sensitivity": 0.0,
  "comment_propensity": 0.0,
  "watch_time_band": "night"
}}

Constraints:
- All affinity numbers must be between 0 and 1.
- primary_categories must contain 1 to 5 categories from the allowed vocabulary.
- category_evidence keys must be from the allowed vocabulary.
- watch_time_band must be one of morning, afternoon, evening, night, mixed.
- Do not output demographic/factual fields such as age, sex, occupation, province, district, country, locale, source_uuid, or virtual_user_id.
- Infer only preference, interest, evidence, and viewing tendency fields from source persona text.
- The LLM generates data only; it does not choose pipeline flow, model routing, or serving policy.
"""
    logger.debug(
        "Built virtual user generation prompt",
        extra={
            "source_uuid": persona.uuid,
            "virtual_user_id": virtual_user_id,
            "prompt_version": PROMPT_VERSION,
            "schema_version": GENERATION_SCHEMA_VERSION,
        },
    )
    return prompt


def parse_virtual_user_json(raw_text: str) -> DerivedVirtualUserFeatures:
    """GLM raw response를 derived-only feature schema로 파싱하고 검증한다."""

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse LLM virtual user JSON", exc_info=True)
        raise ValueError("LLM response must be valid JSON") from exc

    features = DerivedVirtualUserFeatures.model_validate(payload)
    logger.debug(
        "Parsed derived virtual user JSON",
        extra={
            "primary_categories": features.primary_categories,
            "prompt_version": PROMPT_VERSION,
        },
    )
    return features


def _now_iso() -> str:
    """생성 metadata에 넣을 UTC ISO timestamp를 초 단위로 반환한다."""

    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _virtual_user_from_derived_features(
    persona: SourcePersona,
    features: DerivedVirtualUserFeatures,
    virtual_user_id: str,
    model_name: str,
) -> VirtualUser:
    """Source factual field와 GLM derived feature를 병합해 VirtualUser를 만든다."""

    category_affinity = build_category_affinity(
        primary_categories=features.primary_categories,
        category_evidence=features.category_evidence,
        allowed_categories=set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES),
    )
    user = VirtualUser(
        virtual_user_id=virtual_user_id,
        source_uuid=persona.uuid,
        source_hash=persona.source_hash,
        age=persona.age,
        sex=persona.sex,
        age_bucket=age_bucket_for_age(persona.age),
        marital_status=persona.marital_status,
        military_status=persona.military_status,
        family_type=persona.family_type,
        housing_type=persona.housing_type,
        education_level=persona.education_level,
        bachelors_field=persona.bachelors_field,
        occupation=persona.occupation,
        province=persona.province,
        district=persona.district,
        country=persona.country or SOURCE_COUNTRY,
        locale=persona.locale or SOURCE_LOCALE,
        persona_summary=features.persona_summary,
        hobby_keywords=features.hobby_keywords,
        interest_keywords=features.interest_keywords,
        lifestyle_keywords=features.lifestyle_keywords,
        food_keywords=features.food_keywords,
        travel_keywords=features.travel_keywords,
        career_keywords=features.career_keywords,
        family_context_keywords=features.family_context_keywords,
        category_evidence=features.category_evidence,
        category_affinity=category_affinity,
        source_persona_json=persona.model_dump(),
        youtube_profile={
            "primary_categories": features.primary_categories,
            "shorts_affinity": features.shorts_affinity,
            "longform_affinity": features.longform_affinity,
            "trend_sensitivity": features.trend_sensitivity,
            "comment_propensity": features.comment_propensity,
            "watch_time_band": features.watch_time_band,
        },
        generation_meta={
            "schema_version": GENERATION_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "llm_model": model_name,
            "generated_at": _now_iso(),
        },
    )
    return user


class RuleBasedVirtualUserGenerator:
    """LLM 없이 테스트/fixture 용도로 VirtualUser를 생성하는 deterministic generator."""

    def __init__(self, model_name: str = "fixture-rule-generator") -> None:
        """생성 metadata에 기록할 fixture model 이름을 설정한다."""

        self.model_name = model_name

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        """간단한 keyword rule로 derived feature를 만들고 VirtualUser로 병합한다."""

        interests = extract_virtual_user_interests(persona)
        text = " ".join(
            [
                persona.persona,
                persona.hobbies_and_interests,
                persona.professional_persona,
                persona.arts_persona,
            ]
        ).lower()

        if "game" in text or "gaming" in text or "게임" in text:
            categories = ["Gaming", "Music"]
            shorts = 0.84
            longform = 0.42
            trend = 0.78
            comments = 0.38
            band = "night"
        elif "study" in text or "learning" in text or "학습" in text:
            categories = ["Education", "Science & Technology"]
            shorts = 0.43
            longform = 0.76
            trend = 0.45
            comments = 0.22
            band = "evening"
        else:
            categories = ["Music", "Entertainment"]
            shorts = 0.68
            longform = 0.51
            trend = 0.61
            comments = 0.25
            band = "mixed"

        evidence_keywords = (interests.hobby_keywords + interests.interest_keywords)[:3]
        features = DerivedVirtualUserFeatures(
            persona_summary=persona.persona[:180] or "20s Korean virtual user.",
            hobby_keywords=interests.hobby_keywords,
            interest_keywords=interests.interest_keywords,
            lifestyle_keywords=[
                value for value in [persona.family_type, persona.housing_type] if value
            ],
            food_keywords=[persona.culinary_persona] if persona.culinary_persona else [],
            travel_keywords=[persona.travel_persona] if persona.travel_persona else [],
            career_keywords=[persona.career_goals_and_ambitions]
            if persona.career_goals_and_ambitions
            else [],
            family_context_keywords=[persona.family_persona]
            if persona.family_persona
            else [],
            primary_categories=categories,
            category_evidence={
                category: evidence_keywords
                for category in categories
            },
            shorts_affinity=shorts,
            longform_affinity=longform,
            trend_sensitivity=trend,
            comment_propensity=comments,
            watch_time_band=band,
        )
        user = _virtual_user_from_derived_features(
            persona=persona,
            features=features,
            virtual_user_id=virtual_user_id,
            model_name=self.model_name,
        )
        logger.info(
            "Generated fixture virtual user",
            extra={
                "source_uuid": persona.uuid,
                "virtual_user_id": virtual_user_id,
                "sex": persona.sex,
                "categories": categories,
                "model_name": self.model_name,
            },
        )
        return user


class GLMVirtualUserGenerator:
    """OpenAI-compatible Z.ai GLM API를 호출해 VirtualUser를 생성하는 generator."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = DEFAULT_GLM_MODEL,
        base_url: str | None = None,
    ) -> None:
        """API key, model, base URL을 설정하고 GLM 호출 가능 상태인지 검증한다."""

        self.api_key = api_key or os.environ.get("ZAI_API_KEY")
        self.base_url = base_url or os.environ.get("ZAI_BASE_URL") or DEFAULT_ZAI_BASE_URL
        self.model_name = model_name
        if not self.api_key:
            raise ValueError("ZAI_API_KEY is required when use_llm=true")

    def _client_kwargs(self) -> dict[str, object]:
        """OpenAI client 초기화에 필요한 인증/endpoint 인자를 반환한다."""

        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
        }

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        """GLM에 derived JSON을 요청하고 SourcePersona와 병합해 VirtualUser를 반환한다."""

        from openai import OpenAI

        logger.info(
            "Requesting GLM virtual user generation",
            extra={
                "source_uuid": persona.uuid,
                "virtual_user_id": virtual_user_id,
                "model_name": self.model_name,
                "base_url": self.base_url,
            },
        )
        client = OpenAI(**self._client_kwargs())
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {
                    "role": "system",
                    "content": GLM_SYSTEM_HARNESS,
                },
                {
                    "role": "user",
                    "content": build_virtual_user_prompt(persona, virtual_user_id),
                }
            ],
            response_format={"type": "json_object"},
        )
        raw_text = response.choices[0].message.content or ""
        user = _virtual_user_from_derived_features(
            persona=persona,
            features=parse_virtual_user_json(raw_text),
            virtual_user_id=virtual_user_id,
            model_name=self.model_name,
        )

        logger.info(
            "Generated GLM virtual user",
            extra={
                "source_uuid": persona.uuid,
                "virtual_user_id": virtual_user_id,
                "model_name": self.model_name,
            },
        )
        return user
