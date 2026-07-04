import json
import logging

import pytest

import autoresearch.virtual_users.persona_source as persona_source
from autoresearch.virtual_users.persona_source import (
    build_fixture_persona_records,
    build_fixture_raw_persona_records,
    load_raw_persona_records,
    normalize_sex,
    record_age,
    record_sex,
    sample_personas_by_contract,
    sample_raw_personas_by_contract,
    source_persona_from_record,
)


def test_normalize_sex_accepts_common_values():
    assert normalize_sex("male") == "male"
    assert normalize_sex("M") == "male"
    assert normalize_sex("남성") == "male"
    assert normalize_sex("남자") == "male"
    assert normalize_sex("female") == "female"
    assert normalize_sex("F") == "female"
    assert normalize_sex("여성") == "female"
    assert normalize_sex("여자") == "female"


def test_normalize_sex_rejects_unknown_value():
    with pytest.raises(ValueError, match="Unsupported sex value"):
        normalize_sex("unknown")


def test_source_persona_from_record_preserves_expected_fields():
    persona = source_persona_from_record(
        {
            "uuid": "raw-001",
            "age": 26,
            "sex": "F",
            "occupation": "designer",
            "province": "Seoul",
            "district": "Jongno-gu",
            "persona": "Design-focused media user.",
            "hobbies_and_interests": "music, lifestyle",
            "professional_persona": "Early-career designer.",
            "sports_persona": None,
            "arts_persona": "Enjoys visual culture.",
            "cultural_background": "Korean urban context.",
            "skills_and_expertise": "Figma, illustration",
            "travel_persona": "Weekend Seoul gallery visitor.",
            "culinary_persona": "Finds dessert cafes through video.",
            "family_persona": "Shares short videos with siblings.",
        }
    )

    assert persona.uuid == "raw-001"
    assert persona.sex == "female"
    assert persona.sports_persona == ""
    assert persona.arts_persona == "Enjoys visual culture."
    assert persona.skills_and_expertise == "Figma, illustration"
    assert persona.travel_persona == "Weekend Seoul gallery visitor."
    assert persona.culinary_persona == "Finds dessert cafes through video."
    assert persona.family_persona == "Shares short videos with siblings."


def test_source_persona_from_record_preserves_all_raw_columns():
    raw = {
        "uuid": "raw-001",
        "professional_persona": "책을 큐레이션한다.",
        "sports_persona": "숲길을 걷는다.",
        "arts_persona": "동물의 숲과 LP 음악을 좋아한다.",
        "travel_persona": "조용한 해변을 찾는다.",
        "culinary_persona": "마라탕을 주문한다.",
        "family_persona": "혼자 거주한다.",
        "persona": "제주의 작은 서점에서 일한다.",
        "cultural_background": "제주 지역 맥락.",
        "skills_and_expertise": "책 큐레이션",
        "skills_and_expertise_list": "['책 큐레이션', '고객 응대']",
        "hobbies_and_interests": "사려니숲길 산책, 닌텐도 스위치",
        "hobbies_and_interests_list": "['사려니숲길 산책', '닌텐도 스위치']",
        "career_goals_and_ambitions": "교육 공방을 열고 싶어 한다.",
        "sex": "여자",
        "age": 22,
        "marital_status": "미혼",
        "military_status": "비현역",
        "family_type": "혼자 거주",
        "housing_type": "아파트",
        "education_level": "4년제 대학교",
        "bachelors_field": "교육",
        "occupation": "서적·문구 및 음반 판매원",
        "district": "제주-제주시",
        "province": "제주",
        "country": "대한민국",
        "country_code": "KR-SOURCE",
        "locale": "ko-KR-source",
    }

    persona = source_persona_from_record(raw)

    assert persona.sex == "female"
    assert persona.raw_payload["sex"] == "여자"
    assert "sex_normalized" not in persona.model_dump()
    assert persona.country == "대한민국"
    assert persona.country_code == "KR-SOURCE"
    assert persona.locale == "ko-KR-source"
    assert persona.skills_and_expertise_list == ["책 큐레이션", "고객 응대"]
    assert persona.hobbies_and_interests_list == ["사려니숲길 산책", "닌텐도 스위치"]
    assert persona.career_goals_and_ambitions == "교육 공방을 열고 싶어 한다."
    assert persona.marital_status == "미혼"
    assert persona.military_status == "비현역"
    assert persona.family_type == "혼자 거주"
    assert persona.housing_type == "아파트"
    assert persona.education_level == "4년제 대학교"
    assert persona.bachelors_field == "교육"
    assert persona.raw_payload["uuid"] == "raw-001"
    assert persona.source_hash
    assert "교육 공방을 열고 싶어 한다." in persona.source_text


def test_sample_personas_by_contract_returns_requested_20s_counts(caplog):
    records = build_fixture_persona_records(male_count=60, female_count=60)

    with caplog.at_level(logging.INFO, logger="autoresearch.virtual_users.persona_source"):
        sampled = sample_personas_by_contract(
            records=records,
            age_min=20,
            age_max=29,
            male_count=50,
            female_count=50,
            seed=7,
        )

    assert len(sampled) == 100
    assert sum(1 for record in sampled if record.sex == "male") == 50
    assert sum(1 for record in sampled if record.sex == "female") == 50
    assert all(20 <= record.age <= 29 for record in sampled)
    assert "Sampled source personas for virtual user generation" in caplog.text


def test_sample_personas_by_contract_is_deterministic():
    records = build_fixture_persona_records(male_count=60, female_count=60)

    first = sample_personas_by_contract(records, 20, 29, 5, 5, seed=123)
    second = sample_personas_by_contract(records, 20, 29, 5, 5, seed=123)

    assert [record.uuid for record in first] == [record.uuid for record in second]


def test_sample_personas_by_contract_raises_when_not_enough_records():
    records = build_fixture_persona_records(male_count=3, female_count=3)

    with pytest.raises(ValueError, match="Not enough male personas"):
        sample_personas_by_contract(records, 20, 29, 5, 1, seed=1)


def test_write_raw_persona_records_creates_jsonl_snapshot(tmp_path):
    output_path = tmp_path / "raw_personas.jsonl"
    raw_records = [
        {
            "uuid": "p-001",
            "age": 24,
            "sex": "female",
            "occupation": "student",
            "persona": "A student interested in music.",
        },
        {
            "uuid": "p-002",
            "age": 25,
            "sex": "male",
            "occupation": "developer",
            "persona": "A developer interested in gaming.",
        },
    ]

    persona_source.write_raw_persona_records(raw_records, output_path)

    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["uuid"] == "p-001"
    assert json.loads(lines[1])["uuid"] == "p-002"


def test_source_persona_from_record_maps_spec_fields():
    persona = source_persona_from_record(
        {
            "uuid": "p-001",
            "age": 24,
            "sex": "female",
            "occupation": "student",
            "province": "Seoul",
            "district": "Mapo-gu",
            "persona": "Enjoys music videos.",
            "hobbies_and_interests": "music, beauty",
            "hobbies_and_interests_list": ["music", "beauty"],
            "professional_persona": "Learner.",
            "skills_and_expertise": "design",
            "sports_persona": "Light sports viewer.",
            "arts_persona": "Music fan.",
            "travel_persona": "Cafe trips.",
            "culinary_persona": "Cooking shorts.",
            "family_persona": "Family lifestyle.",
            "cultural_background": "Korean urban media user.",
        }
    )

    assert persona.country == "KR"
    assert persona.locale == "ko-KR"
    assert persona.hobbies_and_interests_list == ["music", "beauty"]
    assert persona.skills_and_expertise == "design"
    assert persona.travel_persona == "Cafe trips."
    assert persona.culinary_persona == "Cooking shorts."
    assert persona.family_persona == "Family lifestyle."


def test_load_nvidia_persona_records_can_write_raw_snapshot(monkeypatch, tmp_path):
    raw_records = [
        {
            "uuid": "p-001",
            "age": 24,
            "sex": "female",
            "occupation": "student",
            "persona": "Music fan.",
        },
        {
            "uuid": "p-002",
            "age": 25,
            "sex": "male",
            "occupation": "developer",
            "persona": "Gaming fan.",
        },
    ]

    def fake_load_dataset(name, split, streaming):
        assert name == "nvidia/Nemotron-Personas-Korea"
        assert split == "train"
        assert streaming is True
        return raw_records

    monkeypatch.setattr(persona_source, "load_dataset", fake_load_dataset, raising=False)

    output_path = tmp_path / "raw_snapshot.jsonl"
    records = persona_source.load_nvidia_persona_records(
        max_records=2,
        raw_output_path=output_path,
    )

    assert [record.uuid for record in records] == ["p-001", "p-002"]
    lines = output_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 2
    assert json.loads(lines[0])["uuid"] == "p-001"


def _raw_rows():
    rows = []
    for i in range(60):
        rows.append({"uuid": f"m-{i}", "age": 20 + (i % 10), "sex": "남자"})
    for i in range(60):
        rows.append({"uuid": f"f-{i}", "age": 20 + (i % 10), "sex": "여자"})
    return rows


def test_record_age_and_sex_read_from_raw_dict():
    assert record_age({"age": "25"}) == 25
    assert record_age({"age": None}) is None
    assert record_sex({"sex": "여자"}) == "female"
    assert record_sex({"sex": "unknown"}) is None


def test_sample_raw_personas_by_contract_returns_balanced_seeded_sample():
    rows = _raw_rows()

    sample = sample_raw_personas_by_contract(
        records=rows, age_min=20, age_max=29, male_count=50, female_count=50, seed=42
    )
    again = sample_raw_personas_by_contract(
        records=rows, age_min=20, age_max=29, male_count=50, female_count=50, seed=42
    )

    assert len(sample) == 100
    assert sum(1 for r in sample if record_sex(r) == "male") == 50
    assert sum(1 for r in sample if record_sex(r) == "female") == 50
    assert [r["uuid"] for r in sample] == [r["uuid"] for r in again]  # deterministic


def test_build_fixture_raw_persona_records_returns_balanced_raw_dicts():
    rows = build_fixture_raw_persona_records(male_count=3, female_count=2)

    assert len(rows) == 5
    assert all(isinstance(r, dict) for r in rows)
    assert sum(1 for r in rows if r["sex"] == "남자") == 3
    assert sum(1 for r in rows if r["sex"] == "여자") == 2
    assert rows[0]["uuid"]
    assert rows[0]["persona"]


def test_load_raw_persona_records_returns_raw_dicts_and_snapshot(monkeypatch, tmp_path):
    fake = [{"uuid": "a", "age": 24, "sex": "여자", "persona": "p"}]

    def fake_load_dataset(*args, **kwargs):
        return iter(fake)

    monkeypatch.setattr(
        "autoresearch.virtual_users.persona_source.load_dataset", fake_load_dataset
    )
    snapshot = tmp_path / "raw.jsonl"

    rows = load_raw_persona_records(max_records=1, raw_output_path=snapshot)

    assert rows == fake
    assert snapshot.read_text(encoding="utf-8").strip()
