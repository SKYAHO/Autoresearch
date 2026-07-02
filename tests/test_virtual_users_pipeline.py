import json
import logging

from autoresearch.virtual_users.gemini_generator import RuleBasedVirtualUserGenerator
from autoresearch.virtual_users.persona_source import build_fixture_persona_records
from autoresearch.virtual_users.pipeline import generate_virtual_user_batch
from autoresearch.virtual_users.schema import GenerationRequest


def test_generate_virtual_user_batch_writes_expected_100_user_json(tmp_path, caplog):
    records = build_fixture_persona_records(male_count=60, female_count=60)
    output_path = tmp_path / "virtual_users_20s_100.json"
    warehouse_output_path = tmp_path / "virtual_users_kr.jsonl"
    request = GenerationRequest(
        male_count=50,
        female_count=50,
        seed=11,
        use_gemini=False,
        source_mode="fixture",
        output_path=str(output_path),
        warehouse_output_path=str(warehouse_output_path),
    )

    with caplog.at_level(logging.INFO, logger="autoresearch.virtual_users.pipeline"):
        batch = generate_virtual_user_batch(
            request=request,
            records=records,
            generator=RuleBasedVirtualUserGenerator(),
        )

    assert output_path.exists()
    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["summary"] == {"total": 100, "male": 50, "female": 50}
    assert len(payload["users"]) == 100
    assert batch.summary["male"] == 50
    assert batch.summary["female"] == 50
    assert "Starting virtual user batch generation" in caplog.text
    assert "Wrote virtual user batch output" in caplog.text


def test_generate_virtual_user_batch_uses_stable_virtual_user_ids(tmp_path):
    records = build_fixture_persona_records(male_count=10, female_count=10)
    request = GenerationRequest(
        male_count=2,
        female_count=2,
        seed=3,
        use_gemini=False,
        source_mode="fixture",
        output_path=str(tmp_path / "users.json"),
        warehouse_output_path=str(tmp_path / "warehouse_users.jsonl"),
    )

    batch = generate_virtual_user_batch(
        request=request,
        records=records,
        generator=RuleBasedVirtualUserGenerator(),
    )

    assert [user.virtual_user_id for user in batch.users] == [
        "vu_0001",
        "vu_0002",
        "vu_0003",
        "vu_0004",
    ]


def test_generate_virtual_user_batch_preserves_request_in_output(tmp_path):
    records = build_fixture_persona_records(male_count=5, female_count=5)
    output_path = tmp_path / "users.json"
    warehouse_output_path = tmp_path / "warehouse_users.jsonl"
    request = GenerationRequest(
        age_min=20,
        age_max=29,
        male_count=1,
        female_count=1,
        seed=99,
        use_gemini=False,
        source_mode="fixture",
        output_path=str(output_path),
        warehouse_output_path=str(warehouse_output_path),
    )

    generate_virtual_user_batch(
        request=request,
        records=records,
        generator=RuleBasedVirtualUserGenerator(),
    )

    payload = json.loads(output_path.read_text(encoding="utf-8"))
    assert payload["request"]["male_count"] == 1
    assert payload["request"]["female_count"] == 1
    assert payload["request"]["seed"] == 99
    assert payload["source_dataset"] == "nvidia/Nemotron-Personas-Korea"


def test_generate_virtual_user_batch_writes_warehouse_jsonl(tmp_path):
    records = build_fixture_persona_records(male_count=5, female_count=5)
    batch_output_path = tmp_path / "virtual_users_batch.json"
    warehouse_output_path = tmp_path / "virtual_users_kr.jsonl"
    request = GenerationRequest(
        male_count=1,
        female_count=1,
        seed=7,
        use_gemini=False,
        source_mode="fixture",
        output_path=str(batch_output_path),
        warehouse_output_path=str(warehouse_output_path),
    )

    generate_virtual_user_batch(
        request=request,
        records=records,
        generator=RuleBasedVirtualUserGenerator(),
    )

    lines = warehouse_output_path.read_text(encoding="utf-8").splitlines()
    rows = [json.loads(line) for line in lines]

    assert len(rows) == 2
    assert rows[0]["user_id"].startswith("vu_")
    assert rows[0]["source_dataset"] == "nvidia/Nemotron-Personas-Korea"
    assert rows[0]["country"] == "KR"
    assert rows[0]["locale"] == "ko-KR"
    assert rows[0]["age_bucket"] == "20s"
    assert isinstance(rows[0]["interest_keywords"], list)
    assert "primary_categories" in rows[0]
    assert "watch_time_band" in rows[0]
