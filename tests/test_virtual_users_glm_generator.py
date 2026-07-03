import json
import logging
import sys
import types

import pytest

from autoresearch.virtual_users.glm_generator import (
    GLMVirtualUserGenerator,
    RuleBasedVirtualUserGenerator,
    _ensure_source_persona_matches_user,
    _stamp_generation_meta,
    build_virtual_user_prompt,
    parse_virtual_user_json,
)
from autoresearch.virtual_users.persona_source import build_fixture_persona_records
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    age_bucket_for_age,
)


def _valid_virtual_user_payload() -> dict[str, object]:
    return {
        "virtual_user_id": "vu_0001",
        "source_uuid": "fixture-m-000",
        "age": 20,
        "sex": "male",
        "age_bucket": "20s",
        "occupation": "student",
        "province": "Seoul",
        "district": "Mapo-gu",
        "country": "KR",
        "locale": "ko-KR",
        "persona_summary": "Gaming-focused student.",
        "hobby_keywords": ["gaming", "music"],
        "interest_keywords": ["creator videos", "short-form video"],
        "category_affinity": {"Gaming": 0.86, "Music": 0.74},
        "youtube_profile": {
            "primary_categories": ["Gaming", "Music"],
            "shorts_affinity": 0.86,
            "longform_affinity": 0.34,
            "trend_sensitivity": 0.82,
            "comment_propensity": 0.41,
            "watch_time_band": "night",
        },
    }


def test_build_virtual_user_prompt_contains_glm_json_contract(caplog):
    persona = build_fixture_persona_records(male_count=1, female_count=0)[0]

    with caplog.at_level(
        logging.DEBUG,
        logger="autoresearch.virtual_users.glm_generator",
    ):
        prompt = build_virtual_user_prompt(persona, virtual_user_id="vu_0001")

    assert PROMPT_VERSION in prompt
    assert GENERATION_SCHEMA_VERSION in prompt
    assert "Return only JSON" in prompt
    assert "youtube_profile" in prompt
    assert "shorts_affinity" in prompt
    assert "hobby_keywords" in prompt
    assert "interest_keywords" in prompt
    assert "category_affinity" in prompt
    assert "district" in prompt
    assert "country" in prompt
    assert "locale" in prompt
    assert "generation_meta" not in prompt
    assert persona.uuid in prompt
    assert "Built virtual user generation prompt" in caplog.text


def test_parse_virtual_user_json_accepts_valid_payload(caplog):
    raw = json.dumps(_valid_virtual_user_payload())

    with caplog.at_level(
        logging.DEBUG,
        logger="autoresearch.virtual_users.glm_generator",
    ):
        user = parse_virtual_user_json(raw)

    assert user.virtual_user_id == "vu_0001"
    assert user.youtube_profile.primary_categories == ["Gaming", "Music"]
    assert user.hobby_keywords == ["gaming", "music"]
    assert user.category_affinity["Gaming"] == 0.86
    assert user.generation_meta.llm_model == "unstamped-llm-response"
    assert "Parsed virtual user JSON" in caplog.text


def test_parse_virtual_user_json_rejects_non_json_text():
    with pytest.raises(ValueError, match="LLM response must be valid JSON"):
        parse_virtual_user_json("not json")


def test_stamp_generation_meta_overrides_llm_controlled_metadata():
    payload = _valid_virtual_user_payload()
    payload["generation_meta"] = {
        "schema_version": "wrong-schema",
        "prompt_version": "wrong-prompt",
        "llm_model": "wrong-model",
        "generated_at": "1999-01-01T00:00:00Z",
    }
    user = parse_virtual_user_json(json.dumps(payload))

    stamped = _stamp_generation_meta(user, model_name="glm-5.2")

    assert stamped.generation_meta.schema_version == GENERATION_SCHEMA_VERSION
    assert stamped.generation_meta.prompt_version == PROMPT_VERSION
    assert stamped.generation_meta.llm_model == "glm-5.2"
    assert stamped.generation_meta.generated_at != "1999-01-01T00:00:00Z"


def test_rule_based_generator_produces_valid_schema_without_api_call(caplog):
    persona = build_fixture_persona_records(male_count=1, female_count=0)[0]
    generator = RuleBasedVirtualUserGenerator(model_name="fixture")

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.virtual_users.glm_generator",
    ):
        user = generator.generate(persona, virtual_user_id="vu_0001")

    assert user.virtual_user_id == "vu_0001"
    assert user.source_uuid == persona.uuid
    assert user.sex == "male"
    assert user.age_bucket == age_bucket_for_age(persona.age)
    assert user.district == persona.district
    assert user.country == "KR"
    assert user.locale == "ko-KR"
    assert user.hobby_keywords
    assert user.interest_keywords
    assert user.category_affinity
    assert user.generation_meta.prompt_version == PROMPT_VERSION
    assert user.generation_meta.llm_model == "fixture"
    assert "Generated fixture virtual user" in caplog.text


def test_ensure_source_persona_matches_user_rejects_hallucinated_persona_fields():
    persona = build_fixture_persona_records(male_count=1, female_count=0)[0]
    user = RuleBasedVirtualUserGenerator().generate(persona, virtual_user_id="vu_0001")
    mutated = user.model_copy(update={"sex": "female"})

    with pytest.raises(ValueError, match="sex"):
        _ensure_source_persona_matches_user(
            mutated,
            persona=persona,
            virtual_user_id="vu_0001",
        )


def test_glm_generator_requires_zai_api_key(monkeypatch):
    monkeypatch.delenv("ZAI_API_KEY", raising=False)

    with pytest.raises(ValueError, match="ZAI_API_KEY"):
        GLMVirtualUserGenerator()


def test_glm_generator_uses_zai_base_url_env(monkeypatch):
    monkeypatch.setenv("ZAI_API_KEY", "test-api-key")
    monkeypatch.setenv("ZAI_BASE_URL", "https://example.test/v4")

    generator = GLMVirtualUserGenerator()

    assert generator.api_key == "test-api-key"
    assert generator.base_url == "https://example.test/v4"


def test_glm_generator_calls_openai_compatible_client(monkeypatch):
    captured: dict[str, object] = {}

    class FakeCompletions:
        def create(self, **kwargs):
            captured["completion_kwargs"] = kwargs
            return types.SimpleNamespace(
                choices=[
                    types.SimpleNamespace(
                        message=types.SimpleNamespace(
                            content=json.dumps(_valid_virtual_user_payload())
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    persona = build_fixture_persona_records(male_count=1, female_count=0)[0]
    generator = GLMVirtualUserGenerator(
        api_key="test-api-key",
        base_url="https://example.test/v4",
        model_name="glm-test",
    )

    user = generator.generate(persona, virtual_user_id="vu_0001")

    assert captured["client_kwargs"] == {
        "api_key": "test-api-key",
        "base_url": "https://example.test/v4",
    }
    completion_kwargs = captured["completion_kwargs"]
    assert completion_kwargs["model"] == "glm-test"
    assert completion_kwargs["response_format"] == {"type": "json_object"}
    assert completion_kwargs["messages"][0]["role"] == "user"
    assert "Source persona:" in completion_kwargs["messages"][0]["content"]
    assert user.generation_meta.llm_model == "glm-test"
