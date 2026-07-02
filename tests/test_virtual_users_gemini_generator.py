import json
import logging

import pytest

from autoresearch.virtual_users.gemini_generator import (
    GeminiVirtualUserGenerator,
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
    SOURCE_DATASET,
    SourcePersona,
)


def test_build_virtual_user_prompt_contains_fixed_json_contract(caplog):
    persona = build_fixture_persona_records(male_count=1, female_count=0)[0]

    with caplog.at_level(
        logging.DEBUG,
        logger="autoresearch.virtual_users.gemini_generator",
    ):
        prompt = build_virtual_user_prompt(persona, virtual_user_id="vu_0001")

    assert PROMPT_VERSION in prompt
    assert GENERATION_SCHEMA_VERSION in prompt
    assert "Return only JSON" in prompt
    assert "youtube_profile" in prompt
    assert "shorts_affinity" in prompt
    assert persona.uuid in prompt
    assert "Built virtual user generation prompt" in caplog.text


def test_build_virtual_user_prompt_uses_source_age_bucket():
    persona = SourcePersona(
        uuid="p-034",
        age=34,
        sex="female",
        occupation="designer",
        province="Seoul",
        persona="Design-focused media user.",
    )

    prompt = build_virtual_user_prompt(persona, virtual_user_id="vu_0034")

    assert '"age": 34' in prompt
    assert '"age_bucket": "30s"' in prompt


def test_parse_virtual_user_json_accepts_valid_payload(caplog):
    raw = json.dumps(
        {
            "virtual_user_id": "vu_0001",
            "source_uuid": "fixture-m-000",
            "age": 24,
            "sex": "male",
            "age_bucket": "20s",
            "occupation": "student",
            "province": "Seoul",
            "persona_summary": "Gaming-focused student.",
            "youtube_profile": {
                "primary_categories": ["Gaming", "Music"],
                "shorts_affinity": 0.86,
                "longform_affinity": 0.34,
                "trend_sensitivity": 0.82,
                "comment_propensity": 0.41,
                "watch_time_band": "night",
            },
            "generation_meta": {
                "schema_version": GENERATION_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "llm_model": "gemini-2.0-flash",
                "generated_at": "2026-06-28T00:00:00Z",
            },
        }
    )

    with caplog.at_level(
        logging.DEBUG,
        logger="autoresearch.virtual_users.gemini_generator",
    ):
        user = parse_virtual_user_json(raw)

    assert user.virtual_user_id == "vu_0001"
    assert user.youtube_profile.primary_categories == ["Gaming", "Music"]
    assert "Parsed virtual user JSON" in caplog.text


def test_parse_virtual_user_json_allows_missing_generation_meta_before_stamp():
    raw = json.dumps(
        {
            "virtual_user_id": "vu_0001",
            "source_uuid": "fixture-m-000",
            "age": 24,
            "sex": "male",
            "age_bucket": "20s",
            "occupation": "student",
            "province": "Seoul",
            "persona_summary": "Gaming-focused student.",
            "youtube_profile": {
                "primary_categories": ["Gaming", "Music"],
                "shorts_affinity": 0.86,
                "longform_affinity": 0.34,
                "trend_sensitivity": 0.82,
                "comment_propensity": 0.41,
                "watch_time_band": "night",
            },
        }
    )

    user = parse_virtual_user_json(raw)
    stamped = _stamp_generation_meta(user, model_name="gemini-2.5-flash")

    assert stamped.generation_meta.schema_version == GENERATION_SCHEMA_VERSION
    assert stamped.generation_meta.prompt_version == PROMPT_VERSION
    assert stamped.generation_meta.llm_model == "gemini-2.5-flash"


def test_parse_virtual_user_json_rejects_non_json_text():
    with pytest.raises(ValueError, match="Gemini response must be valid JSON"):
        parse_virtual_user_json("not json")


def test_stamp_generation_meta_overrides_llm_controlled_metadata():
    user = parse_virtual_user_json(
        json.dumps(
            {
                "virtual_user_id": "vu_0001",
                "source_uuid": "fixture-m-000",
                "age": 24,
                "sex": "male",
                "age_bucket": "20s",
                "occupation": "student",
                "province": "Seoul",
                "persona_summary": "Gaming-focused student.",
                "youtube_profile": {
                    "primary_categories": ["Gaming", "Music"],
                    "shorts_affinity": 0.86,
                    "longform_affinity": 0.34,
                    "trend_sensitivity": 0.82,
                    "comment_propensity": 0.41,
                    "watch_time_band": "night",
                },
                "generation_meta": {
                    "schema_version": "wrong-schema",
                    "prompt_version": "wrong-prompt",
                    "llm_model": "gpt-4o",
                    "generated_at": "1999-01-01T00:00:00Z",
                },
            }
        )
    )

    stamped = _stamp_generation_meta(user, model_name="gemini-2.5-flash")

    assert stamped.generation_meta.schema_version == GENERATION_SCHEMA_VERSION
    assert stamped.generation_meta.prompt_version == PROMPT_VERSION
    assert stamped.generation_meta.llm_model == "gemini-2.5-flash"
    assert stamped.generation_meta.generated_at != "1999-01-01T00:00:00Z"


def test_rule_based_generator_produces_valid_schema_without_api_call(caplog):
    persona = build_fixture_persona_records(male_count=1, female_count=0)[0]
    generator = RuleBasedVirtualUserGenerator(model_name="fixture")

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.virtual_users.gemini_generator",
    ):
        user = generator.generate(persona, virtual_user_id="vu_0001")

    assert user.virtual_user_id == "vu_0001"
    assert user.source_uuid == persona.uuid
    assert user.sex == "male"
    assert user.age_bucket == "20s"
    assert user.generation_meta.prompt_version == PROMPT_VERSION
    assert user.generation_meta.llm_model == "fixture"
    assert "Generated fixture virtual user" in caplog.text


def test_rule_based_generator_populates_warehouse_fields():
    persona = SourcePersona(
        uuid="p-001",
        age=24,
        sex="female",
        occupation="student",
        province="Seoul",
        district="Mapo-gu",
        persona="Enjoys music videos and lifestyle creators.",
        hobbies_and_interests="beauty, study videos",
        hobbies_and_interests_list=["music", "beauty"],
    )

    user = RuleBasedVirtualUserGenerator().generate(persona, virtual_user_id="vu_0001")

    assert user.virtual_user_id == "vu_0001"
    assert user.source_uuid == "p-001"
    assert user.source_dataset == SOURCE_DATASET
    assert user.country == "KR"
    assert user.locale == "ko-KR"
    assert user.district == "Mapo-gu"
    assert user.interest_keywords == ["music", "beauty", "study", "lifestyle"]


def test_rule_based_generator_derives_age_bucket_from_age():
    persona = SourcePersona(
        uuid="p-034",
        age=34,
        sex="female",
        occupation="designer",
        province="Seoul",
        persona="Design-focused media user.",
    )

    user = RuleBasedVirtualUserGenerator().generate(
        persona,
        virtual_user_id="vu_0034",
    )

    assert user.age_bucket == "30s"


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


def test_gemini_generator_requires_api_key(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_APPLICATION_CREDENTIALS", raising=False)

    with pytest.raises(ValueError, match="GEMINI_API_KEY, GOOGLE_API_KEY, or GOOGLE_APPLICATION_CREDENTIALS"):
        GeminiVirtualUserGenerator()


def test_gemini_generator_accepts_adc_credentials_path(monkeypatch, tmp_path):
    credentials_path = tmp_path / "application_default_credentials.json"
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials_path))

    generator = GeminiVirtualUserGenerator()

    assert generator.auth_mode == "vertex_adc"
    assert generator.credentials_path == str(credentials_path)
    assert generator._client_kwargs() == {
        "vertexai": True,
        "location": "global",
    }


def test_gemini_generator_prefers_api_key_over_adc(monkeypatch, tmp_path):
    credentials_path = tmp_path / "application_default_credentials.json"
    monkeypatch.setenv("GEMINI_API_KEY", "test-api-key")
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.setenv("GOOGLE_APPLICATION_CREDENTIALS", str(credentials_path))

    generator = GeminiVirtualUserGenerator()

    assert generator.auth_mode == "api_key"
    assert generator._client_kwargs() == {"api_key": "test-api-key"}
