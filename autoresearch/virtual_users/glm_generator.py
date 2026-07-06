"""GLM/OpenRouter LLM 또는 fixture rule로 raw persona dict를 VirtualUser로 변환한다."""

import hashlib
import json
import logging
import os
from datetime import UTC, datetime

from autoresearch.virtual_users.categories import DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
from autoresearch.virtual_users.persona_source import record_age, record_sex
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_COUNTRY,
    SOURCE_LOCALE,
    VirtualUser,
    age_bucket_for_age,
)


logger = logging.getLogger(__name__)

DEFAULT_GLM_MODEL = "glm-5.2"
DEFAULT_ZAI_BASE_URL = "https://api.z.ai/api/coding/paas/v4"
DEFAULT_OPENROUTER_MODEL = "mistralai/mistral-nemo"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
GLM_SYSTEM_HARNESS = """너는 virtual user row generator다.
아래 원본 persona를 근거로 지정된 JSON schema를 채운다.
없는 정보를 지어내지 마라. 원본에서 추론 가능한 수준만 생성하라.
category 값은 제공된 allowed category vocabulary 안에서만 선택하라.
virtual_user_id, source_uuid, source_hash, source_persona_json, age_bucket, generation_meta, country, locale는 만들지 마라(코드가 채운다).
출력은 지정된 JSON 하나만 허용한다. Markdown이나 주석을 넣지 마라.
"""


def build_source_hash(record: dict) -> str:
    """raw row의 안정적 추적 hash."""
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def build_virtual_user_prompt(raw_row: dict, virtual_user_id: str) -> str:
    """raw persona dict 전체와 허용 vocab을 GLM user prompt로 만든다."""
    allowed = "\n".join(f"- {c}" for c in DEFAULT_KAGGLE_YOUTUBE_CATEGORIES)
    prompt = f"""You convert a Korean persona into a virtual YouTube user row.

Prompt version: {PROMPT_VERSION}
Schema version: {GENERATION_SCHEMA_VERSION}

Source persona (raw):
{json.dumps(raw_row, ensure_ascii=False, indent=2)}

Allowed category vocabulary:
{allowed}

Return only JSON with this shape (no Markdown, no commentary):
{{
  "age": 24, "sex": "female",
  "occupation": "", "province": "", "district": "",
  "marital_status": "", "military_status": "", "family_type": "",
  "housing_type": "", "education_level": "", "bachelors_field": "",
  "persona_summary": "one sentence",
  "hobby_keywords": [], "interest_keywords": [], "lifestyle_keywords": [],
  "food_keywords": [], "travel_keywords": [], "career_keywords": [],
  "family_context_keywords": [],
  "youtube_profile": {{
    "primary_categories": ["Music"],
    "watch_time_band": "night"
  }}
}}

Constraints:
- sex must be "male" or "female".
- primary_categories: 1 to 5 items from the allowed vocabulary.
- watch_time_band in [morning, afternoon, evening, night, mixed].
"""
    logger.debug(
        "Built virtual user prompt",
        extra={"virtual_user_id": virtual_user_id, "prompt_version": PROMPT_VERSION},
    )
    return prompt


def _now_iso() -> str:
    """생성 metadata에 넣을 UTC ISO timestamp를 초 단위로 반환한다."""

    return datetime.now(UTC).replace(microsecond=0).isoformat()


def assemble_virtual_user(
    raw_row: dict,
    raw_text: str,
    virtual_user_id: str,
    model_name: str,
) -> VirtualUser:
    """LLM content를 parse하고 코드 stamp 필드를 얹어 VirtualUser로 검증한다."""
    payload = json.loads(raw_text)  # raises json.JSONDecodeError -> invalid_json

    payload["virtual_user_id"] = virtual_user_id
    payload["source_uuid"] = str(raw_row.get("uuid", ""))
    payload["source_hash"] = build_source_hash(raw_row)
    # 얕은 복사로 records 리스트/QuarantineRecord와의 우발적 공유 mutation을 예방한다.
    payload["source_persona_json"] = dict(raw_row)
    # 인구통계는 raw persona를 source-of-truth로 stamp한다. 샘플러가 raw age/sex로
    # 균형(20대·성비 50:50)을 보장하므로, LLM이 흘린 age/sex를 채택하면 출력 분포가
    # 그 계약과 어긋난다. raw에 값이 없으면 schema_fail로 격리한다(샘플된 행은 항상 값 존재).
    raw_age = record_age(raw_row)
    raw_sex = record_sex(raw_row)
    if raw_age is None or raw_sex is None:
        raise ValueError(f"raw persona lacks usable age/sex: uuid={raw_row.get('uuid')}")
    payload["age"] = raw_age
    payload["sex"] = raw_sex
    payload["age_bucket"] = age_bucket_for_age(raw_age)
    # country/locale는 다른 stamp 필드처럼 무조건 덮어쓴다(harness가 "코드가 채운다"고
    # 지시하므로 LLM이 값을 뱉어도 채택하지 않는다).
    payload["country"] = SOURCE_COUNTRY
    payload["locale"] = SOURCE_LOCALE
    payload["generation_meta"] = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "llm_model": model_name,
        "generated_at": _now_iso(),
    }
    # raises ValidationError -> schema_fail. Stamping above may also raise
    # ValueError (raw lacks age/sex) or TypeError/AttributeError for a non-object
    # payload such as a JSON list/number/string/null -> schema_fail.
    return VirtualUser.model_validate(payload)


class RuleBasedVirtualUserGenerator:
    """LLM 없이 전체 content JSON을 만드는 deterministic fixture generator."""

    def __init__(self, model_name: str = "fixture-rule-generator") -> None:
        """생성 metadata에 기록할 fixture model 이름을 설정한다."""

        self.model_name = model_name

    def generate(self, raw_row: dict, virtual_user_id: str) -> str:
        """간단한 keyword rule로 전체 content JSON 문자열을 만든다."""

        text = " ".join(
            str(raw_row.get(k, ""))
            for k in ("persona", "hobbies_and_interests", "occupation")
        ).lower()
        if "game" in text or "게임" in text:
            categories, band = ["Gaming", "Music"], "night"
        elif "study" in text or "학습" in text:
            categories, band = ["Education", "Science & Technology"], "evening"
        else:
            categories, band = ["Music", "Entertainment"], "mixed"

        content = {
            "age": int(raw_row.get("age", 20)),
            "sex": record_sex(raw_row) or "male",
            "occupation": str(raw_row.get("occupation", "")),
            "province": str(raw_row.get("province", "")),
            "district": str(raw_row.get("district", "")),
            "marital_status": str(raw_row.get("marital_status", "")),
            "military_status": str(raw_row.get("military_status", "")),
            "family_type": str(raw_row.get("family_type", "")),
            "housing_type": str(raw_row.get("housing_type", "")),
            "education_level": str(raw_row.get("education_level", "")),
            "bachelors_field": str(raw_row.get("bachelors_field", "")),
            "persona_summary": str(raw_row.get("persona", ""))[:180] or "20s KR user.",
            "hobby_keywords": [], "interest_keywords": [], "lifestyle_keywords": [],
            "food_keywords": [], "travel_keywords": [], "career_keywords": [],
            "family_context_keywords": [],
            "youtube_profile": {
                "primary_categories": categories,
                "watch_time_band": band,
            },
        }
        logger.info(
            "Generated fixture virtual user content",
            extra={
                "source_uuid": raw_row.get("uuid", ""),
                "virtual_user_id": virtual_user_id,
                "categories": categories,
                "model_name": self.model_name,
            },
        )
        return json.dumps(content, ensure_ascii=False)


class _OpenAICompatibleVirtualUserGenerator:
    """OpenAI-compatible chat completions로 raw persona → VirtualUser JSON을 만드는 공통 로직.

    provider(Z.ai GLM / OpenRouter)별로 api_key·base_url·model만 다르고, system harness와
    프롬프트·호출 방식(json_object 강제)은 동일하다. 서브클래스가 provider 설정을 채운다.
    """

    def __init__(self, api_key: str | None, base_url: str, model_name: str) -> None:
        self.api_key = api_key
        self.base_url = base_url
        self.model_name = model_name

    def _client_kwargs(self) -> dict[str, object]:
        """OpenAI client 초기화에 필요한 인증/endpoint 인자를 반환한다."""

        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
        }

    def generate(self, raw_row: dict, virtual_user_id: str) -> str:
        """provider에 raw row 기반 full-schema JSON을 요청하고 raw text를 그대로 반환한다."""

        from openai import OpenAI

        logger.info(
            "Requesting virtual user generation",
            extra={
                "source_uuid": raw_row.get("uuid", ""),
                "virtual_user_id": virtual_user_id,
                "model_name": self.model_name,
                "base_url": self.base_url,
            },
        )
        client = OpenAI(**self._client_kwargs())
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": GLM_SYSTEM_HARNESS},
                {
                    "role": "user",
                    "content": build_virtual_user_prompt(raw_row, virtual_user_id),
                },
            ],
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""


class GLMVirtualUserGenerator(_OpenAICompatibleVirtualUserGenerator):
    """OpenAI-compatible Z.ai GLM API generator."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = DEFAULT_GLM_MODEL,
        base_url: str | None = None,
    ) -> None:
        """ZAI_API_KEY/ZAI_BASE_URL 환경변수로 GLM 호출 상태를 구성·검증한다."""

        super().__init__(
            api_key=api_key or os.environ.get("ZAI_API_KEY"),
            base_url=base_url or os.environ.get("ZAI_BASE_URL") or DEFAULT_ZAI_BASE_URL,
            model_name=model_name,
        )
        if not self.api_key:
            raise ValueError("ZAI_API_KEY is required when use_llm=true")


class OpenRouterVirtualUserGenerator(_OpenAICompatibleVirtualUserGenerator):
    """OpenAI-compatible OpenRouter API generator (기본 모델 mistral-nemo)."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = DEFAULT_OPENROUTER_MODEL,
        base_url: str | None = None,
    ) -> None:
        """OPENROUTER_API_KEY/OPENROUTER_BASE_URL 환경변수로 OpenRouter 호출 상태를 구성·검증한다."""

        super().__init__(
            api_key=api_key or os.environ.get("OPENROUTER_API_KEY"),
            base_url=base_url or os.environ.get("OPENROUTER_BASE_URL") or DEFAULT_OPENROUTER_BASE_URL,
            model_name=model_name,
        )
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is required when use_llm=true")
