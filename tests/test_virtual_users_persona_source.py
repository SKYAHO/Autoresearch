import json

import pytest

import autoresearch.virtual_users.persona_source as persona_source
from autoresearch.virtual_users.persona_source import (
    build_fixture_raw_persona_records,
    load_raw_persona_records,
    normalize_sex,
    record_age,
    record_sex,
    sample_raw_personas_by_contract,
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
