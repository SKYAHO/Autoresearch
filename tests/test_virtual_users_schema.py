import logging

import pytest
from pydantic import ValidationError

from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    GenerationRequest,
    SourcePersona,
    VirtualUser,
    VirtualUserBatch,
    age_bucket_for_age,
)


def test_generation_request_defaults_match_mvp_contract():
    request = GenerationRequest()

    assert request.age_min == 20
    assert request.age_max == 29
    assert request.male_count == 50
    assert request.female_count == 50
    assert request.seed == 42
    assert request.use_llm is True
    assert request.max_concurrency == 1
    assert request.output_path == "asset/virtual_user/virtual_users_20s_100.parquet"


def test_generation_request_rejects_invalid_max_concurrency():
    with pytest.raises(ValidationError):
        GenerationRequest(max_concurrency=0)


def test_age_bucket_for_age_uses_source_age():
    assert age_bucket_for_age(19) == "10s"
    assert age_bucket_for_age(20) == "20s"
    assert age_bucket_for_age(29) == "20s"
    assert age_bucket_for_age(30) == "30s"


def test_source_persona_normalizes_required_fields():
    persona = SourcePersona(
        uuid="p-001",
        age=24,
        sex="male",
        occupation="student",
        province="Seoul",
        district="Gangnam-gu",
        persona="A student who enjoys games and music.",
        hobbies_and_interests="gaming, music, short videos",
    )

    assert persona.uuid == "p-001"
    assert persona.age == 24
    assert persona.sex == "male"
    assert persona.hobbies_and_interests == "gaming, music, short videos"


def test_virtual_user_schema_accepts_expected_json_shape():
    user = VirtualUser(
        virtual_user_id="vu_0001",
        source_uuid="p-001",
        age=24,
        sex="male",
        age_bucket="20s",
        occupation="student",
        province="Seoul",
        district="Gangnam-gu",
        country="KR",
        locale="ko-KR",
        persona_summary="Trend-sensitive college student who watches gaming videos.",
        hobby_keywords=["gaming", "music"],
        interest_keywords=["creator economy", "short videos"],
        category_affinity={"Gaming": 0.91, "Music": 0.74},
        youtube_profile={
            "primary_categories": ["Gaming", "Music"],
            "shorts_affinity": 0.82,
            "longform_affinity": 0.41,
            "trend_sensitivity": 0.76,
            "comment_propensity": 0.35,
            "watch_time_band": "night",
        },
        generation_meta={
            "schema_version": GENERATION_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "llm_model": "glm-5.2",
            "generated_at": "2026-06-28T00:00:00Z",
        },
    )

    assert user.youtube_profile.shorts_affinity == 0.82
    assert user.country == "KR"
    assert user.locale == "ko-KR"
    assert user.hobby_keywords == ["gaming", "music"]
    assert user.category_affinity["Gaming"] == 0.91
    assert user.generation_meta.prompt_version == PROMPT_VERSION


def test_virtual_user_schema_rejects_out_of_range_category_affinity():
    with pytest.raises(ValidationError):
        VirtualUser(
            virtual_user_id="vu_0001",
            source_uuid="p-001",
            age=24,
            sex="female",
            age_bucket="20s",
            occupation="designer",
            province="Seoul",
            district="Jongno-gu",
            country="KR",
            locale="ko-KR",
            persona_summary="Designer persona.",
            hobby_keywords=["visual culture"],
            interest_keywords=["design"],
            category_affinity={"Music": 1.2},
            youtube_profile={
                "primary_categories": ["Music"],
                "shorts_affinity": 0.7,
                "longform_affinity": 0.4,
                "trend_sensitivity": 0.5,
                "comment_propensity": 0.3,
                "watch_time_band": "evening",
            },
            generation_meta={
                "schema_version": GENERATION_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "llm_model": "glm-5.2",
                "generated_at": "2026-06-28T00:00:00Z",
            },
        )


def test_virtual_user_schema_rejects_out_of_range_affinity():
    with pytest.raises(ValidationError):
        VirtualUser(
            virtual_user_id="vu_0001",
            source_uuid="p-001",
            age=24,
            sex="female",
            age_bucket="20s",
            occupation="designer",
            province="Seoul",
            district="Jongno-gu",
            country="KR",
            locale="ko-KR",
            persona_summary="Designer persona.",
            hobby_keywords=["music"],
            interest_keywords=["design"],
            category_affinity={"Music": 0.7},
            youtube_profile={
                "primary_categories": ["Music"],
                "shorts_affinity": 1.2,
                "longform_affinity": 0.4,
                "trend_sensitivity": 0.5,
                "comment_propensity": 0.3,
                "watch_time_band": "evening",
            },
            generation_meta={
                "schema_version": GENERATION_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "llm_model": "glm-5.2",
                "generated_at": "2026-06-28T00:00:00Z",
            },
        )


def test_virtual_user_batch_counts_users_by_sex(caplog):
    users = [
        VirtualUser(
            virtual_user_id="vu_0001",
            source_uuid="p-001",
            age=22,
            sex="male",
            age_bucket="20s",
            occupation="student",
            province="Seoul",
            district="Mapo-gu",
            country="KR",
            locale="ko-KR",
            persona_summary="Male student.",
            hobby_keywords=["gaming"],
            interest_keywords=["music"],
            category_affinity={"Gaming": 0.8},
            youtube_profile={
                "primary_categories": ["Gaming"],
                "shorts_affinity": 0.8,
                "longform_affinity": 0.4,
                "trend_sensitivity": 0.7,
                "comment_propensity": 0.3,
                "watch_time_band": "night",
            },
            generation_meta={
                "schema_version": GENERATION_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "llm_model": "fixture",
                "generated_at": "2026-06-28T00:00:00Z",
            },
        ),
        VirtualUser(
            virtual_user_id="vu_0002",
            source_uuid="p-002",
            age=23,
            sex="female",
            age_bucket="20s",
            occupation="marketer",
            province="Busan",
            district="Haeundae-gu",
            country="KR",
            locale="ko-KR",
            persona_summary="Female marketer.",
            hobby_keywords=["music"],
            interest_keywords=["lifestyle"],
            category_affinity={"Music": 0.7},
            youtube_profile={
                "primary_categories": ["Music"],
                "shorts_affinity": 0.7,
                "longform_affinity": 0.5,
                "trend_sensitivity": 0.6,
                "comment_propensity": 0.2,
                "watch_time_band": "evening",
            },
            generation_meta={
                "schema_version": GENERATION_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "llm_model": "fixture",
                "generated_at": "2026-06-28T00:00:00Z",
            },
        ),
    ]

    batch = VirtualUserBatch(
        schema_version=GENERATION_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        source_dataset="nvidia/Nemotron-Personas-Korea",
        request=GenerationRequest(male_count=1, female_count=1),
        users=users,
    )
    with caplog.at_level(logging.DEBUG, logger="autoresearch.virtual_users.schema"):
        payload = batch.to_output_dict()

    assert batch.summary["total"] == 2
    assert batch.summary["male"] == 1
    assert batch.summary["female"] == 1
    assert payload["summary"] == {"total": 2, "male": 1, "female": 1}
    assert "Prepared virtual user batch output" in caplog.text


def test_source_persona_accepts_spec_fields_and_kr_defaults():
    persona = SourcePersona(
        uuid="p-001",
        age=24,
        sex="female",
        occupation="student",
        province="Seoul",
        district="Mapo-gu",
        persona="A student who enjoys music and lifestyle videos.",
        hobbies_and_interests="music, beauty, lifestyle",
        hobbies_and_interests_list=["music", "beauty"],
        professional_persona="Early career learner.",
        skills_and_expertise="presentation, design",
        sports_persona="Light sports highlights viewer.",
        arts_persona="Interested in popular music.",
        travel_persona="Enjoys Seoul cafe trip videos.",
        culinary_persona="Watches cooking shorts.",
        family_persona="Lives with family.",
    )

    assert persona.country == "KR"
    assert persona.locale == "ko-KR"
    assert persona.hobbies_and_interests_list == ["music", "beauty"]
    assert persona.skills_and_expertise == "presentation, design"
    assert persona.travel_persona == "Enjoys Seoul cafe trip videos."
    assert persona.culinary_persona == "Watches cooking shorts."
    assert persona.family_persona == "Lives with family."


def test_virtual_user_exports_warehouse_ready_row():
    user = VirtualUser(
        virtual_user_id="vu_0001",
        source_uuid="p-001",
        source_dataset="nvidia/Nemotron-Personas-Korea",
        country="KR",
        locale="ko-KR",
        age=24,
        sex="female",
        age_bucket="20s",
        occupation="student",
        province="Seoul",
        district="Mapo-gu",
        persona_summary="Student interested in music and lifestyle.",
        interest_keywords=["music", "beauty", "lifestyle"],
        youtube_profile={
            "primary_categories": ["Music", "Howto & Style"],
            "shorts_affinity": 0.82,
            "longform_affinity": 0.38,
            "trend_sensitivity": 0.71,
            "comment_propensity": 0.24,
            "watch_time_band": "night",
        },
        generation_meta={
            "schema_version": GENERATION_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "llm_model": "fixture",
            "generated_at": "2026-07-01T00:00:00+00:00",
        },
    )

    row = user.to_warehouse_row()

    assert row == {
        "user_id": "vu_0001",
        "source_uuid": "p-001",
        "source_dataset": "nvidia/Nemotron-Personas-Korea",
        "country": "KR",
        "locale": "ko-KR",
        "age": 24,
        "sex": "female",
        "occupation": "student",
        "province": "Seoul",
        "district": "Mapo-gu",
        "persona_summary": "Student interested in music and lifestyle.",
        "hobby_keywords": [],
        "interest_keywords": ["music", "beauty", "lifestyle"],
        "category_affinity": {},
        "primary_categories": ["Music", "Howto & Style"],
        "shorts_affinity": 0.82,
        "longform_affinity": 0.38,
        "trend_sensitivity": 0.71,
        "comment_propensity": 0.24,
        "watch_time_band": "night",
        "schema_version": GENERATION_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "llm_model": "fixture",
        "generated_at": "2026-07-01T00:00:00+00:00",
    }
