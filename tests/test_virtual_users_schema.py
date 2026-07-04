import logging

import pytest
from pydantic import ValidationError

from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    DerivedVirtualUserFeatures,
    GenerationRequest,
    GenerationResult,
    QuarantineRecord,
    SourcePersona,
    VirtualUser,
    VirtualUserBatch,
    age_bucket_for_age,
)


def _make_single_user_batch() -> VirtualUserBatch:
    """1명짜리 VirtualUser로 구성된 VirtualUserBatch를 만드는 테스트 헬퍼."""

    user = VirtualUser(
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
    )

    return VirtualUserBatch(
        schema_version=GENERATION_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        source_dataset="nvidia/Nemotron-Personas-Korea",
        request=GenerationRequest(male_count=1, female_count=0),
        users=[user],
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


def test_source_persona_preserves_full_raw_persona_contract():
    persona = SourcePersona(
        uuid="p-001",
        professional_persona="책 큐레이션을 잘한다.",
        sports_persona="숲길을 천천히 걷는다.",
        arts_persona="동물의 숲과 LP 음악을 좋아한다.",
        travel_persona="조용한 해변과 골목 카페를 찾는다.",
        culinary_persona="마라탕과 꿔바로우를 주문한다.",
        family_persona="혼자 살지만 가족과 정서적 유대가 있다.",
        persona="제주의 작은 서점에서 일하는 22세 여성.",
        cultural_background="제주시 아파트 단지에서 성장했다.",
        skills_and_expertise="독립출판물 큐레이션",
        skills_and_expertise_list=["독립출판물 큐레이션", "고객 응대"],
        hobbies_and_interests="사려니숲길 산책, 닌텐도 스위치",
        hobbies_and_interests_list=["사려니숲길 산책", "닌텐도 스위치"],
        career_goals_and_ambitions="작은 교육 공방을 운영하고 싶어 한다.",
        sex="female",
        age=22,
        marital_status="미혼",
        military_status="비현역",
        family_type="혼자 거주",
        housing_type="아파트",
        education_level="4년제 대학교",
        bachelors_field="교육",
        occupation="서적·문구 및 음반 판매원",
        district="제주-제주시",
        province="제주",
        country="대한민국",
        country_code="KR",
        locale="ko-KR",
        source_text="제주의 작은 서점에서 일하는 22세 여성.",
        source_hash="hash",
        raw_payload={"uuid": "p-001", "sex": "여자"},
    )

    assert persona.sex == "female"
    assert persona.career_goals_and_ambitions == "작은 교육 공방을 운영하고 싶어 한다."
    assert persona.education_level == "4년제 대학교"
    assert persona.raw_payload["uuid"] == "p-001"
    assert persona.raw_payload["sex"] == "여자"
    assert "sex_normalized" not in persona.model_dump()


def test_derived_virtual_user_features_accepts_glm_only_payload():
    features = DerivedVirtualUserFeatures(
        persona_summary="제주 서점에서 일하며 조용한 취미를 즐긴다.",
        hobby_keywords=["사려니숲길 산책", "닌텐도 스위치"],
        interest_keywords=["책 큐레이션", "LP 음악"],
        lifestyle_keywords=["혼자 거주", "저녁 휴식"],
        food_keywords=["마라탕", "꿔바로우"],
        travel_keywords=["조용한 해변", "골목 카페"],
        career_keywords=["교육 공방"],
        family_context_keywords=["가족 밑반찬"],
        primary_categories=["Gaming", "Music"],
        category_evidence={"Gaming": ["닌텐도 스위치"], "Music": ["LP 음악"]},
        shorts_affinity=0.62,
        longform_affinity=0.57,
        trend_sensitivity=0.41,
        comment_propensity=0.18,
        watch_time_band="night",
    )

    assert features.primary_categories == ["Gaming", "Music"]
    assert features.category_evidence["Gaming"] == ["닌텐도 스위치"]


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


def test_virtual_user_exports_lossless_warehouse_row():
    user = VirtualUser(
        virtual_user_id="vu_0001",
        source_uuid="p-001",
        source_hash="hash-001",
        age=22,
        sex="female",
        age_bucket="20s",
        marital_status="미혼",
        military_status="비현역",
        family_type="혼자 거주",
        housing_type="아파트",
        education_level="4년제 대학교",
        bachelors_field="교육",
        occupation="서적·문구 및 음반 판매원",
        province="제주",
        district="제주-제주시",
        country="KR",
        locale="ko-KR",
        persona_summary="제주 서점에서 일하며 교육 공방을 꿈꾼다.",
        hobby_keywords=["닌텐도 스위치"],
        interest_keywords=["책 큐레이션"],
        lifestyle_keywords=["혼자 거주"],
        food_keywords=["마라탕"],
        travel_keywords=["조용한 해변"],
        career_keywords=["교육 공방"],
        family_context_keywords=["가족 밑반찬"],
        category_evidence={"Gaming": ["닌텐도 스위치"]},
        category_affinity={"Gaming": 0.88},
        source_persona_json={"uuid": "p-001", "country": "대한민국"},
        youtube_profile={
            "primary_categories": ["Gaming"],
            "shorts_affinity": 0.62,
            "longform_affinity": 0.57,
            "trend_sensitivity": 0.41,
            "comment_propensity": 0.18,
            "watch_time_band": "night",
        },
        generation_meta={
            "schema_version": GENERATION_SCHEMA_VERSION,
            "prompt_version": PROMPT_VERSION,
            "llm_model": "glm-5.2",
            "generated_at": "2026-06-28T00:00:00Z",
        },
    )

    row = user.to_warehouse_row()

    assert row["source_hash"] == "hash-001"
    assert row["education_level"] == "4년제 대학교"
    assert row["career_keywords"] == ["교육 공방"]
    assert row["category_evidence"]["Gaming"] == ["닌텐도 스위치"]
    assert row["source_persona_json"]["uuid"] == user.source_uuid


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
        "source_hash": "",
        "country": "KR",
        "locale": "ko-KR",
        "age": 24,
        "sex": "female",
        "marital_status": "",
        "military_status": "",
        "family_type": "",
        "housing_type": "",
        "education_level": "",
        "bachelors_field": "",
        "occupation": "student",
        "province": "Seoul",
        "district": "Mapo-gu",
        "persona_summary": "Student interested in music and lifestyle.",
        "hobby_keywords": [],
        "interest_keywords": ["music", "beauty", "lifestyle"],
        "lifestyle_keywords": [],
        "food_keywords": [],
        "travel_keywords": [],
        "career_keywords": [],
        "family_context_keywords": [],
        "category_affinity": {},
        "primary_categories": ["Music", "Howto & Style"],
        "category_evidence": {},
        "shorts_affinity": 0.82,
        "longform_affinity": 0.38,
        "trend_sensitivity": 0.71,
        "comment_propensity": 0.24,
        "watch_time_band": "night",
        "source_persona_json": {},
        "schema_version": GENERATION_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "llm_model": "fixture",
        "generated_at": "2026-07-01T00:00:00+00:00",
    }


def test_quarantine_record_captures_failure_context():
    record = QuarantineRecord(
        source_uuid="p-1",
        raw_row={"uuid": "p-1", "sex": "여자"},
        raw_llm_response="{bad json",
        error_type="invalid_json",
        error_message="Expecting value",
    )
    assert record.error_type == "invalid_json"
    assert record.raw_row["sex"] == "여자"


def test_generation_result_summary_counts_valid_and_quarantine():
    result = GenerationResult(
        batch=_make_single_user_batch(),
        quarantine=[
            QuarantineRecord(
                source_uuid="p-2",
                raw_row={"uuid": "p-2"},
                raw_llm_response="",
                error_type="api_error",
                error_message="timeout",
            )
        ],
    )
    assert result.summary == {"valid": 1, "quarantined": 1, "api_error": 1,
                              "invalid_json": 0, "schema_fail": 0}
