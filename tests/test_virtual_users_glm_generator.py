import json
import sys
import types

import pytest

from autoresearch.virtual_users.glm_generator import (
    GLM_SYSTEM_HARNESS,
    GLMVirtualUserGenerator,
    RuleBasedVirtualUserGenerator,
    assemble_virtual_user,
    build_source_hash,
    build_virtual_user_prompt,
)
from autoresearch.virtual_users.persona_source import build_fixture_raw_persona_records
from autoresearch.virtual_users.schema import VirtualUser


def _raw_row():
    return {"uuid": "p-001", "age": 24, "sex": "여자", "persona": "제주 서점 직원"}


def _full_content():
    return {
        "age": 24,
        "sex": "female",
        "occupation": "판매원",
        "province": "제주",
        "district": "제주시",
        "marital_status": "미혼",
        "military_status": "비현역",
        "family_type": "1인 가구",
        "housing_type": "아파트",
        "education_level": "4년제 대학교",
        "bachelors_field": "교육",
        "persona_summary": "제주의 20대 여성.",
        "hobby_keywords": ["독서"],
        "interest_keywords": ["음악"],
        "lifestyle_keywords": [],
        "food_keywords": [],
        "travel_keywords": [],
        "career_keywords": [],
        "family_context_keywords": [],
        "category_evidence": {"Music": ["LP"]},
        "category_affinity": {"Music": 0.8},
        "youtube_profile": {
            "primary_categories": ["Music"],
            "shorts_affinity": 0.6,
            "longform_affinity": 0.5,
            "trend_sensitivity": 0.4,
            "comment_propensity": 0.2,
            "watch_time_band": "night",
        },
    }


def test_build_virtual_user_prompt_embeds_raw_row_and_vocab():
    prompt = build_virtual_user_prompt(_raw_row(), "vu_0001")
    assert "p-001" in prompt          # raw row 포함
    assert "제주 서점 직원" in prompt
    assert "Music" in prompt          # allowed vocabulary
    assert "sex_normalized" not in prompt


def test_assemble_virtual_user_stamps_code_owned_fields():
    user = assemble_virtual_user(
        raw_row=_raw_row(),
        raw_text=json.dumps(_full_content(), ensure_ascii=False),
        virtual_user_id="vu_0001",
        model_name="glm-5.2",
    )
    assert isinstance(user, VirtualUser)
    assert user.virtual_user_id == "vu_0001"
    assert user.source_uuid == "p-001"                 # code-stamped from raw row
    assert user.source_hash == build_source_hash(_raw_row())
    assert user.age_bucket == "20s"                    # code-computed from age
    assert user.source_persona_json == _raw_row()       # raw row preserved
    assert user.generation_meta.llm_model == "glm-5.2"
    assert user.sex == "female"                         # LLM content


def test_assemble_virtual_user_raises_on_invalid_json():
    with pytest.raises(json.JSONDecodeError):
        assemble_virtual_user(_raw_row(), "{not json", "vu_0001", "glm-5.2")


def test_assemble_virtual_user_raises_on_schema_violation():
    from pydantic import ValidationError

    bad = _full_content()
    bad["youtube_profile"]["shorts_affinity"] = 5.0  # out of range
    with pytest.raises(ValidationError):
        assemble_virtual_user(
            _raw_row(), json.dumps(bad), "vu_0001", "glm-5.2"
        )


def test_rule_based_generator_returns_assemblable_full_content():
    gen = RuleBasedVirtualUserGenerator()
    raw = {"uuid": "p-9", "age": 24, "sex": "여자", "persona": "게임을 좋아함",
           "hobbies_and_interests": "게임, 음악"}

    raw_text = gen.generate(raw, "vu_0001")
    user = assemble_virtual_user(raw, raw_text, "vu_0001", gen.model_name)

    assert user.sex == "female"
    assert user.youtube_profile.primary_categories
    assert user.category_affinity
    assert "sex_normalized" not in user.source_persona_json


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
                            content="{\"ok\": true}"
                        )
                    )
                ]
            )

    class FakeOpenAI:
        def __init__(self, **kwargs):
            captured["client_kwargs"] = kwargs
            self.chat = types.SimpleNamespace(completions=FakeCompletions())

    monkeypatch.setitem(sys.modules, "openai", types.SimpleNamespace(OpenAI=FakeOpenAI))
    raw = build_fixture_raw_persona_records(male_count=1, female_count=0)[0]
    generator = GLMVirtualUserGenerator(
        api_key="test-api-key",
        base_url="https://example.test/v4",
        model_name="glm-test",
    )

    result = generator.generate(raw, virtual_user_id="vu_0001")

    assert result == "{\"ok\": true}"
    assert captured["client_kwargs"] == {
        "api_key": "test-api-key",
        "base_url": "https://example.test/v4",
    }
    completion_kwargs = captured["completion_kwargs"]
    assert completion_kwargs["model"] == "glm-test"
    assert completion_kwargs["response_format"] == {"type": "json_object"}
    assert completion_kwargs["messages"][0]["role"] == "system"
    assert completion_kwargs["messages"][0]["content"] == GLM_SYSTEM_HARNESS
    assert completion_kwargs["messages"][1]["role"] == "user"
    assert "Source persona (raw):" in completion_kwargs["messages"][1]["content"]
