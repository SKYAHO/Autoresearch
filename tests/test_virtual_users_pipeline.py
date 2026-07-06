import json
import logging

import pyarrow.parquet as pq
import pytest

from autoresearch.virtual_users.glm_generator import RuleBasedVirtualUserGenerator
from autoresearch.virtual_users.persona_source import build_fixture_raw_persona_records
from autoresearch.virtual_users.pipeline import (
    BatchGenerationError,
    generate_virtual_user_batch,
)
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


class _AllBadGenerator(RuleBasedVirtualUserGenerator):
    """모든 호출에서 잘못된 JSON을 반환해 전량 실패를 만든다."""

    def generate(self, raw_row, virtual_user_id):
        return "{not valid json"


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


def test_batch_isolates_non_object_json_row_as_schema_fail(tmp_path):
    """valid JSON이지만 object가 아닌 body(TypeError)를 schema_fail로 격리한다."""

    records = build_fixture_raw_persona_records(male_count=2, female_count=1)
    request = GenerationRequest(
        male_count=2, female_count=1, use_llm=False,
        output_path=str(tmp_path / "vu.parquet"),
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
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )

    result = generate_virtual_user_batch(request, records, _OneBadGenerator())

    assert result.summary["valid"] == 2          # 배치가 안 죽음
    assert result.summary["quarantined"] == 1
    assert result.summary["invalid_json"] == 1
    q_lines = (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(q_lines) == 1
    assert json.loads(q_lines[0])["error_type"] == "invalid_json"


def test_batch_raises_when_quarantine_ratio_exceeds_threshold(tmp_path):
    """전량 실패는 격리 파일을 남기고 BatchGenerationError로 종료한다."""

    records = build_fixture_raw_persona_records(male_count=2, female_count=1)
    quarantine_path = tmp_path / "q.jsonl"
    request = GenerationRequest(
        male_count=2, female_count=1, use_llm=False,
        output_path=str(tmp_path / "vu.parquet"),
        quarantine_output_path=str(quarantine_path),
    )

    with pytest.raises(BatchGenerationError):
        generate_virtual_user_batch(request, records, _AllBadGenerator())

    # 격리 파일은 포렌식용으로 남고, 성공 산출물(parquet)은 쓰이지 않는다.
    assert len(quarantine_path.read_text(encoding="utf-8").splitlines()) == 3
    assert not (tmp_path / "vu.parquet").exists()


def test_batch_tolerates_quarantine_within_threshold(tmp_path):
    """임계치(기본 0.5) 이하 격리는 raise 없이 정상 산출물을 쓴다."""

    records = build_fixture_raw_persona_records(male_count=2, female_count=1)
    request = GenerationRequest(
        male_count=2, female_count=1, use_llm=False,
        output_path=str(tmp_path / "vu.parquet"),
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )

    result = generate_virtual_user_batch(request, records, _OneBadGenerator())

    assert result.summary["quarantined"] == 1  # 1/3 ≈ 0.33 <= 0.5
    assert (tmp_path / "vu.parquet").exists()


def test_generate_virtual_user_batch_writes_expected_100_user_parquet(tmp_path, caplog):
    records = build_fixture_raw_persona_records(male_count=60, female_count=60)
    output_path = tmp_path / "virtual_users_20s_100.parquet"
    quarantine_output_path = tmp_path / "virtual_users_quarantine.jsonl"
    request = GenerationRequest(
        male_count=50,
        female_count=50,
        seed=11,
        use_llm=False,
        source_mode="fixture",
        output_path=str(output_path),
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
    assert rows[0]["user_id"].startswith("vu_")
    assert rows[0]["source_dataset"] == "nvidia/Nemotron-Personas-Korea"
    assert rows[0]["district"]
    assert rows[0]["country"] == "KR"
    assert rows[0]["locale"] == "ko-KR"
    assert rows[0]["hobby_keywords"] is not None
    assert rows[0]["interest_keywords"] is not None
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


def test_generate_virtual_user_batch_uses_stable_virtual_user_ids(tmp_path):
    records = build_fixture_raw_persona_records(male_count=10, female_count=10)
    request = GenerationRequest(
        male_count=2,
        female_count=2,
        seed=3,
        use_llm=False,
        source_mode="fixture",
        output_path=str(tmp_path / "users.parquet"),
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


def test_end_to_end_100_rows_rule_based(tmp_path):
    records = build_fixture_raw_persona_records(male_count=60, female_count=60)
    request = GenerationRequest(
        male_count=50, female_count=50, use_llm=False,
        output_path=str(tmp_path / "vu.parquet"),
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )

    result = generate_virtual_user_batch(request, records, RuleBasedVirtualUserGenerator())

    assert result.summary == {"valid": 100, "quarantined": 0,
                              "api_error": 0, "invalid_json": 0, "schema_fail": 0}
    assert (tmp_path / "vu.parquet").exists()
    rows = pq.read_table(tmp_path / "vu.parquet").to_pylist()
    assert len(rows) == 100
    assert result.batch.summary == {"total": 100, "male": 50, "female": 50}
