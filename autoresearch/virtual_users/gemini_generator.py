"""SourcePersona를 YouTube 추천용 VirtualUser profile로 생성한다."""

import json
import logging
import os
from datetime import UTC, datetime
from typing import Protocol

from autoresearch.virtual_users.interests import extract_interest_keywords
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_DATASET,
    SourcePersona,
    VirtualUser,
    age_bucket_for,
)


logger = logging.getLogger(__name__)

DEFAULT_GEMINI_MODEL = "gemini-2.5-flash"
DEFAULT_VERTEX_LOCATION = "global"
PERSONA_SUMMARY_MAX_CHARS = 180


class VirtualUserGenerator(Protocol):
    """pipeline이 Gemini/rule-based 구현을 같은 방식으로 호출하기 위한 인터페이스."""

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        """SourcePersona 한 건을 VirtualUser 한 건으로 변환한다."""

        ...


def build_virtual_user_prompt(persona: SourcePersona, virtual_user_id: str) -> str:
    """Gemini가 따라야 할 JSON contract와 원천 persona 정보를 prompt로 구성한다."""

    age_bucket = age_bucket_for(persona.age)
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
  "source_dataset": "{SOURCE_DATASET}",
  "country": "{persona.country}",
  "locale": "{persona.locale}",
  "age": {persona.age},
  "sex": "{persona.sex}",
  "age_bucket": "{age_bucket}",
  "occupation": "{persona.occupation}",
  "province": "{persona.province}",
  "district": "{persona.district}",
  "persona_summary": "one Korean or English sentence",
  "interest_keywords": ["music", "gaming"],
  "youtube_profile": {{
    "primary_categories": ["Gaming", "Music"],
    "shorts_affinity": 0.0,
    "longform_affinity": 0.0,
    "trend_sensitivity": 0.0,
    "comment_propensity": 0.0,
    "watch_time_band": "night"
  }},
  "generation_meta": {{
    "schema_version": "{GENERATION_SCHEMA_VERSION}",
    "prompt_version": "{PROMPT_VERSION}",
    "llm_model": "model name",
    "generated_at": "ISO-8601 timestamp"
  }}
}}

Constraints:
- All affinity numbers must be between 0 and 1.
- primary_categories must contain 1 to 5 YouTube categories.
- watch_time_band must be one of morning, afternoon, evening, night, mixed.
- age_bucket must be "{age_bucket}".
- Keep original age, sex, occupation, province, and source_uuid.
- Keep original district, country, locale, and source_uuid.
- interest_keywords must be a list of concise lowercase English keywords.
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
    """LLM 응답 문자열을 VirtualUser schema로 검증 가능한 객체로 파싱한다."""

    try:
        payload = json.loads(raw_text)
    except json.JSONDecodeError as exc:
        logger.warning("Failed to parse Gemini virtual user JSON", exc_info=True)
        raise ValueError("Gemini response must be valid JSON") from exc

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
    """LLM이 바꾸면 안 되는 원천 persona 식별/인구통계 필드를 검증한다."""

    expected = {
        "virtual_user_id": virtual_user_id,
        "source_uuid": persona.uuid,
        "age": persona.age,
        "sex": persona.sex,
        "occupation": persona.occupation,
        "province": persona.province,
        "district": persona.district,
        "country": persona.country,
        "locale": persona.locale,
    }
    actual = {
        "virtual_user_id": user.virtual_user_id,
        "source_uuid": user.source_uuid,
        "age": user.age,
        "sex": user.sex,
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
    """생성 metadata에 사용할 UTC ISO-8601 timestamp를 만든다."""

    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _stamp_generation_meta(user: VirtualUser, model_name: str) -> VirtualUser:
    """LLM 응답의 metadata 대신 pipeline이 신뢰하는 생성 metadata를 덮어쓴다."""

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


def _normalize_interest_keywords_from_persona(
    user: VirtualUser,
    persona: SourcePersona,
) -> VirtualUser:
    payload = user.model_dump()
    payload["interest_keywords"] = extract_interest_keywords(persona)
    return VirtualUser.model_validate(payload)


def _first_env(*names: str) -> str | None:
    """여러 환경변수 후보 중 가장 먼저 설정된 값을 반환한다."""

    for name in names:
        value = os.environ.get(name)
        if value:
            return value
    return None


class RuleBasedVirtualUserGenerator:
    """Gemini 없이도 테스트와 fallback에 사용할 deterministic virtual user generator."""

    def __init__(self, model_name: str = "fixture-rule-generator") -> None:
        """생성 metadata에 기록할 rule-based model 이름을 설정한다."""

        self.model_name = model_name

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        """간단한 keyword rule로 YouTube profile과 관심 keyword를 생성한다."""

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

        interest_keywords = extract_interest_keywords(persona)
        user = VirtualUser(
            virtual_user_id=virtual_user_id,
            source_uuid=persona.uuid,
            source_dataset=SOURCE_DATASET,
            country=persona.country,
            locale=persona.locale,
            age=persona.age,
            sex=persona.sex,
            age_bucket=age_bucket_for(persona.age),
            occupation=persona.occupation,
            province=persona.province,
            district=persona.district,
            persona_summary=(
                persona.persona[:PERSONA_SUMMARY_MAX_CHARS]
                or f"{age_bucket_for(persona.age)} Korean virtual user."
            ),
            interest_keywords=interest_keywords,
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


class GeminiVirtualUserGenerator:
    """Gemini API 또는 Vertex ADC 인증으로 virtual user를 생성하는 adapter."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = DEFAULT_GEMINI_MODEL,
        credentials_path: str | None = None,
        project: str | None = None,
        location: str | None = None,
    ) -> None:
        """API key 또는 Google ADC 인증 정보를 읽어 Gemini client 설정을 준비한다."""

        self.api_key = api_key or _first_env("GEMINI_API_KEY", "GOOGLE_API_KEY")
        self.credentials_path = credentials_path or os.environ.get(
            "GOOGLE_APPLICATION_CREDENTIALS"
        )
        if self.credentials_path:
            os.environ["GOOGLE_APPLICATION_CREDENTIALS"] = self.credentials_path
        self.project = project or os.environ.get("GOOGLE_CLOUD_PROJECT")
        self.location = location or os.environ.get("GOOGLE_CLOUD_LOCATION")
        self.model_name = model_name
        if not self.api_key and not self.credentials_path:
            raise ValueError(
                "GEMINI_API_KEY, GOOGLE_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS "
                "is required when use_gemini=true"
            )
        self.auth_mode = "api_key" if self.api_key else "vertex_adc"

    def _client_kwargs(self) -> dict[str, object]:
        """google-genai Client 생성에 필요한 인증별 keyword arguments를 만든다."""

        if self.api_key:
            return {"api_key": self.api_key}

        kwargs: dict[str, object] = {
            "vertexai": True,
            "location": self.location or DEFAULT_VERTEX_LOCATION,
        }
        if self.project:
            kwargs["project"] = self.project
        return kwargs

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        """Gemini를 호출하고 응답 검증/metadata stamp를 거쳐 VirtualUser를 반환한다."""

        from google import genai
        from google.genai import types

        logger.info(
            "Requesting Gemini virtual user generation",
            extra={
                "source_uuid": persona.uuid,
                "virtual_user_id": virtual_user_id,
                "model_name": self.model_name,
                "auth_mode": self.auth_mode,
            },
        )
        client = genai.Client(**self._client_kwargs())
        response = client.models.generate_content(
            model=self.model_name,
            contents=build_virtual_user_prompt(persona, virtual_user_id),
            config=types.GenerateContentConfig(response_mime_type="application/json"),
        )
        user = _stamp_generation_meta(
            parse_virtual_user_json(response.text or ""),
            model_name=self.model_name,
        )
        user = _normalize_interest_keywords_from_persona(user, persona)
        _ensure_source_persona_matches_user(
            user,
            persona=persona,
            virtual_user_id=virtual_user_id,
        )

        logger.info(
            "Generated Gemini virtual user",
            extra={
                "source_uuid": persona.uuid,
                "virtual_user_id": virtual_user_id,
                "model_name": self.model_name,
            },
        )
        return user
