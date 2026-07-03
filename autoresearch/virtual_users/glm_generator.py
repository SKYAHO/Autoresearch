import json
import logging
import os
from datetime import UTC, datetime
from typing import Protocol

from autoresearch.virtual_users.interests import extract_virtual_user_interests
from autoresearch.virtual_users.schema import (
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


class VirtualUserGenerator(Protocol):
    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        ...


def build_virtual_user_prompt(persona: SourcePersona, virtual_user_id: str) -> str:
    persona_payload = persona.model_dump()
    prompt = f"""You convert a Korean synthetic persona into a virtual YouTube user profile.

Prompt version: {PROMPT_VERSION}
Schema version: {GENERATION_SCHEMA_VERSION}
Virtual user id: {virtual_user_id}

Source persona:
{json.dumps(persona_payload, ensure_ascii=False, indent=2)}

Return only JSON. Do not include Markdown. Do not include commentary.

Required JSON shape:
{{
  "virtual_user_id": "{virtual_user_id}",
  "source_uuid": "{persona.uuid}",
  "age": {persona.age},
  "sex": "{persona.sex}",
  "age_bucket": "{age_bucket_for_age(persona.age)}",
  "occupation": "{persona.occupation}",
  "province": "{persona.province}",
  "district": "{persona.district}",
  "country": "{SOURCE_COUNTRY}",
  "locale": "{SOURCE_LOCALE}",
  "persona_summary": "one Korean or English sentence",
  "hobby_keywords": ["keyword inferred from source hobbies"],
  "interest_keywords": ["keyword inferred from source persona text"],
  "category_affinity": {{
    "Gaming": 0.0,
    "Music": 0.0,
    "Entertainment": 0.0
  }},
  "youtube_profile": {{
    "primary_categories": ["Gaming", "Music"],
    "shorts_affinity": 0.0,
    "longform_affinity": 0.0,
    "trend_sensitivity": 0.0,
    "comment_propensity": 0.0,
    "watch_time_band": "night"
  }}
}}

Constraints:
- All affinity numbers must be between 0 and 1.
- primary_categories must contain 1 to 5 YouTube categories.
- category_affinity values must be between 0 and 1.
- watch_time_band must be one of morning, afternoon, evening, night, mixed.
- Keep original age, sex, occupation, province, district, country, locale, and source_uuid.
- Infer hobby_keywords, interest_keywords, category_affinity, and youtube_profile from source persona text.
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


def parse_virtual_user_json(raw_text: str) -> VirtualUser:
    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse LLM virtual user JSON", exc_info=True)
        raise ValueError("LLM response must be valid JSON") from exc

    payload.pop("generation_meta", None)
    payload["generation_meta"] = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "llm_model": "unstamped-llm-response",
        "generated_at": _now_iso(),
    }
    user = VirtualUser.model_validate(payload)
    logger.debug(
        "Parsed virtual user JSON",
        extra={
            "virtual_user_id": user.virtual_user_id,
            "source_uuid": user.source_uuid,
            "prompt_version": user.generation_meta.prompt_version,
            "llm_model": user.generation_meta.llm_model,
        },
    )
    return user


def _ensure_source_persona_matches_user(
    user: VirtualUser,
    persona: SourcePersona,
    virtual_user_id: str,
) -> None:
    expected = {
        "virtual_user_id": virtual_user_id,
        "source_uuid": persona.uuid,
        "age": persona.age,
        "sex": persona.sex,
        "age_bucket": age_bucket_for_age(persona.age),
        "occupation": persona.occupation,
        "province": persona.province,
        "district": persona.district,
        "country": SOURCE_COUNTRY,
        "locale": SOURCE_LOCALE,
    }
    actual = {
        "virtual_user_id": user.virtual_user_id,
        "source_uuid": user.source_uuid,
        "age": user.age,
        "sex": user.sex,
        "age_bucket": user.age_bucket,
        "occupation": user.occupation,
        "province": user.province,
        "district": user.district,
        "country": user.country,
        "locale": user.locale,
    }
    mismatches = [
        field
        for field, expected_value in expected.items()
        if actual[field] != expected_value
    ]
    if mismatches:
        raise ValueError(
            "Generated user fields do not match source persona: "
            + ", ".join(mismatches)
        )


def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _stamp_generation_meta(user: VirtualUser, model_name: str) -> VirtualUser:
    payload = user.model_dump()
    payload["generation_meta"] = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "llm_model": model_name,
        "generated_at": _now_iso(),
    }
    stamped = VirtualUser.model_validate(payload)
    logger.debug(
        "Stamped deterministic generation metadata",
        extra={
            "virtual_user_id": stamped.virtual_user_id,
            "source_uuid": stamped.source_uuid,
            "model_name": model_name,
            "schema_version": GENERATION_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
        },
    )
    return stamped


class RuleBasedVirtualUserGenerator:
    def __init__(self, model_name: str = "fixture-rule-generator") -> None:
        self.model_name = model_name

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
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

        user = VirtualUser(
            virtual_user_id=virtual_user_id,
            source_uuid=persona.uuid,
            age=persona.age,
            sex=persona.sex,
            age_bucket=age_bucket_for_age(persona.age),
            occupation=persona.occupation,
            province=persona.province,
            district=persona.district,
            country=SOURCE_COUNTRY,
            locale=SOURCE_LOCALE,
            persona_summary=persona.persona[:180] or "20s Korean virtual user.",
            hobby_keywords=interests.hobby_keywords,
            interest_keywords=interests.interest_keywords,
            category_affinity=interests.category_affinity,
            youtube_profile={
                "primary_categories": categories,
                "shorts_affinity": shorts,
                "longform_affinity": longform,
                "trend_sensitivity": trend,
                "comment_propensity": comments,
                "watch_time_band": band,
            },
            generation_meta={
                "schema_version": GENERATION_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "llm_model": self.model_name,
                "generated_at": _now_iso(),
            },
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
    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = DEFAULT_GLM_MODEL,
        base_url: str | None = None,
    ) -> None:
        self.api_key = api_key or os.environ.get("ZAI_API_KEY")
        self.base_url = base_url or os.environ.get("ZAI_BASE_URL") or DEFAULT_ZAI_BASE_URL
        self.model_name = model_name
        if not self.api_key:
            raise ValueError("ZAI_API_KEY is required when use_llm=true")

    def _client_kwargs(self) -> dict[str, object]:
        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
        }

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
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
                    "role": "user",
                    "content": build_virtual_user_prompt(persona, virtual_user_id),
                }
            ],
            response_format={"type": "json_object"},
        )
        raw_text = response.choices[0].message.content or ""
        user = _stamp_generation_meta(
            parse_virtual_user_json(raw_text),
            model_name=self.model_name,
        )
        _ensure_source_persona_matches_user(
            user,
            persona=persona,
            virtual_user_id=virtual_user_id,
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
