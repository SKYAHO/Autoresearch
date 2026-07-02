import json
import logging

import pytest

import autoresearch.virtual_users.persona_source as persona_source
from autoresearch.virtual_users.persona_source import (
    build_fixture_persona_records,
    normalize_sex,
    sample_personas_by_contract,
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
        }
    )

    assert persona.uuid == "raw-001"
    assert persona.sex == "female"
    assert persona.sports_persona == ""
    assert persona.arts_persona == "Enjoys visual culture."


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
