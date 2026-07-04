import json
import logging

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.virtual_users.glm_generator import RuleBasedVirtualUserGenerator
from autoresearch.virtual_users.persona_source import build_fixture_raw_persona_records
from autoresearch.virtual_users.pipeline import generate_virtual_user_batch
from autoresearch.virtual_users.schema import GenerationRequest


class _OneBadGenerator(RuleBasedVirtualUserGenerator):
    """두 번째 호출에서만 잘못된 JSON을 반환한다."""

    def __init__(self):
        super().__init__()
        self._n = 0

    def generate(self, raw_row, virtual_user_id):
        self._n += 1
        if self._n == 2:
            return "{not valid json"
        return super().generate(raw_row, virtual_user_id)


class _NonObjectJsonGenerator(RuleBasedVirtualUserGenerator):
    """두 번째 호출에서만 valid JSON이지만 object가 아닌 body를 반환한다."""

    def __init__(self):
        super().__init__()
        self._n = 0

    def generate(self, raw_row, virtual_user_id):
        self._n += 1
        if self._n == 2:
            return "[]"
        return super().generate(raw_row, virtual_user_id)


def _affinity_map_to_dict(value: object) -> dict[str, float]:
    return {key: score for key, score in value}


def test_batch_isolates_non_object_json_row_as_schema_fail(tmp_path):
    """valid JSON이지만 object가 아닌 body(TypeError)를 schema_fail로 격리한다."""

    records = build_fixture_raw_persona_records(male_count=2, female_count=1)
    request = GenerationRequest(
        male_count=2, female_count=1, use_llm=False,
        output_path=str(tmp_path / "vu.parquet"),
        warehouse_output_path=str(tmp_path / "vu.jsonl"),
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )

    result = generate_virtual_user_batch(request, records, _NonObjectJsonGenerator())

    assert result.summary["valid"] == 2          # 배치가 안 죽음
    assert result.summary["quarantined"] == 1
    assert result.summary["schema_fail"] == 1
    q_lines = (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(q_lines) == 1
    assert json.loads(q_lines[0])["error_type"] == "schema_fail"


def test_batch_isolates_bad_row_and_quarantines_it(tmp_path):
    records = build_fixture_raw_persona_records(male_count=2, female_count=1)
    request = GenerationRequest(
        male_count=2, female_count=1, use_llm=False,
        output_path=str(tmp_path / "vu.parquet"),
        warehouse_output_path=str(tmp_path / "vu.jsonl"),
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )

    result = generate_virtual_user_batch(request, records, _OneBadGenerator())

    assert result.summary["valid"] == 2          # 배치가 안 죽음
    assert result.summary["quarantined"] == 1
    assert result.summary["invalid_json"] == 1
    q_lines = (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(q_lines) == 1
    assert json.loads(q_lines[0])["error_type"] == "invalid_json"


def test_generate_virtual_user_batch_writes_expected_100_user_parquet(tmp_path, caplog):
    records = build_fixture_raw_persona_records(male_count=60, female_count=60)
    output_path = tmp_path / "virtual_users_20s_100.parquet"
    warehouse_output_path = tmp_path / "virtual_users_kr.jsonl"
    quarantine_output_path = tmp_path / "virtual_users_quarantine.jsonl"
    request = GenerationRequest(
        male_count=50,
        female_count=50,
        seed=11,
        use_llm=False,
        source_mode="fixture",
        output_path=str(output_path),
        warehouse_output_path=str(warehouse_output_path),
        quarantine_output_path=str(quarantine_output_path),
    )

    with caplog.at_level(logging.INFO, logger="autoresearch.virtual_users.pipeline"):
        result = generate_virtual_user_batch(
            request=request,
            records=records,
            generator=RuleBasedVirtualUserGenerator(),
        )

    batch = result.batch
    assert result.summary["quarantined"] == 0
    assert output_path.exists()
    rows = pq.read_table(output_path).to_pylist()
    assert len(rows) == 100
    assert sum(1 for row in rows if row["sex"] == "male") == 50
    assert sum(1 for row in rows if row["sex"] == "female") == 50
    assert rows[0]["source_dataset"] == "nvidia/Nemotron-Personas-Korea"
    assert rows[0]["district"]
    assert rows[0]["country"] == "KR"
    assert rows[0]["locale"] == "ko-KR"
    assert rows[0]["hobby_keywords"] is not None
    assert rows[0]["interest_keywords"] is not None
    assert rows[0]["category_affinity"]
    assert isinstance(rows[0]["category_evidence"], str)
    assert json.loads(rows[0]["category_evidence"])
    assert isinstance(rows[0]["source_persona_json"], str)
    source_persona_json = json.loads(rows[0]["source_persona_json"])
    assert source_persona_json["uuid"] == rows[0]["source_uuid"]
    assert "sex_normalized" not in source_persona_json
    assert rows[0]["primary_categories"]
    assert "youtube_primary_categories" not in rows[0]
    assert batch.summary["male"] == 50
    assert batch.summary["female"] == 50
    assert "Starting virtual user batch generation" in caplog.text
    assert "Wrote virtual user batch parquet output" in caplog.text


def test_generate_virtual_user_batch_writes_affinity_as_map_not_sparse_struct(tmp_path):
    records = build_fixture_raw_persona_records(male_count=1, female_count=1)
    output_path = tmp_path / "users.parquet"
    request = GenerationRequest(
        male_count=1,
        female_count=1,
        seed=17,
        use_llm=False,
        source_mode="fixture",
        output_path=str(output_path),
        warehouse_output_path=str(output_path.with_name("warehouse_users.jsonl")),
        quarantine_output_path=str(output_path.with_name("quarantine.jsonl")),
    )

    generate_virtual_user_batch(
        request=request,
        records=records,
        generator=RuleBasedVirtualUserGenerator(),
    )

    table = pq.read_table(output_path)
    assert table.schema.field("category_affinity").type == pa.map_(
        pa.string(),
        pa.float64(),
    )
    assert table.schema.field("category_evidence").type == pa.string()
    assert table.schema.field("source_persona_json").type == pa.string()
    assert table.schema.field("primary_categories").type == pa.list_(pa.string())
    assert "youtube_primary_categories" not in table.schema.names
    rows = table.to_pylist()
    affinities = [_affinity_map_to_dict(row["category_affinity"]) for row in rows]
    assert all(affinity for affinity in affinities)
    assert all(None not in affinity.values() for affinity in affinities)


def test_generate_virtual_user_batch_uses_stable_virtual_user_ids(tmp_path):
    records = build_fixture_raw_persona_records(male_count=10, female_count=10)
    request = GenerationRequest(
        male_count=2,
        female_count=2,
        seed=3,
        use_llm=False,
        source_mode="fixture",
        output_path=str(tmp_path / "users.parquet"),
        warehouse_output_path=str(tmp_path / "warehouse_users.jsonl"),
        quarantine_output_path=str(tmp_path / "quarantine.jsonl"),
    )

    result = generate_virtual_user_batch(
        request=request,
        records=records,
        generator=RuleBasedVirtualUserGenerator(),
    )

    assert [user.virtual_user_id for user in result.batch.users] == [
        "vu_0001",
        "vu_0002",
        "vu_0003",
        "vu_0004",
    ]


def test_generate_virtual_user_batch_preserves_request_metadata_in_parquet(tmp_path):
    records = build_fixture_raw_persona_records(male_count=5, female_count=5)
    output_path = tmp_path / "users.parquet"
    warehouse_output_path = tmp_path / "warehouse_users.jsonl"
    quarantine_output_path = tmp_path / "quarantine.jsonl"
    request = GenerationRequest(
        age_min=20,
        age_max=29,
        male_count=1,
        female_count=1,
        seed=99,
        use_llm=False,
        source_mode="fixture",
        output_path=str(output_path),
        warehouse_output_path=str(warehouse_output_path),
        quarantine_output_path=str(quarantine_output_path),
    )

    generate_virtual_user_batch(
        request=request,
        records=records,
        generator=RuleBasedVirtualUserGenerator(),
    )

    rows = pq.read_table(output_path).to_pylist()
    assert rows[0]["request_male_count"] == 1
    assert rows[0]["request_female_count"] == 1
    assert rows[0]["request_seed"] == 99
    assert rows[0]["request_use_llm"] is False
    assert rows[0]["request_max_concurrency"] == 1
    assert rows[0]["source_dataset"] == "nvidia/Nemotron-Personas-Korea"


def test_generate_virtual_user_batch_writes_warehouse_jsonl(tmp_path):
    records = build_fixture_raw_persona_records(male_count=5, female_count=5)
    batch_output_path = tmp_path / "virtual_users_batch.parquet"
    warehouse_output_path = tmp_path / "virtual_users_kr.jsonl"
    quarantine_output_path = tmp_path / "quarantine.jsonl"
    request = GenerationRequest(
        male_count=1,
        female_count=1,
        seed=7,
        use_llm=False,
        source_mode="fixture",
        output_path=str(batch_output_path),
        warehouse_output_path=str(warehouse_output_path),
        quarantine_output_path=str(quarantine_output_path),
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
    assert isinstance(rows[0]["hobby_keywords"], list)
    assert isinstance(rows[0]["interest_keywords"], list)
    assert isinstance(rows[0]["category_affinity"], dict)
    assert isinstance(rows[0]["category_evidence"], dict)
    assert isinstance(rows[0]["source_persona_json"], dict)
    assert rows[0]["source_persona_json"]["uuid"] == rows[0]["source_uuid"]
    assert "primary_categories" in rows[0]
    assert "watch_time_band" in rows[0]


def test_end_to_end_100_rows_rule_based(tmp_path):
    records = build_fixture_raw_persona_records(male_count=60, female_count=60)
    request = GenerationRequest(
        male_count=50, female_count=50, use_llm=False,
        output_path=str(tmp_path / "vu.parquet"),
        warehouse_output_path=str(tmp_path / "vu.jsonl"),
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )

    result = generate_virtual_user_batch(request, records, RuleBasedVirtualUserGenerator())

    assert result.summary == {"valid": 100, "quarantined": 0,
                              "api_error": 0, "invalid_json": 0, "schema_fail": 0}
    warehouse_lines = (tmp_path / "vu.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(warehouse_lines) == 100
    assert result.batch.summary == {"total": 100, "male": 50, "female": 50}
