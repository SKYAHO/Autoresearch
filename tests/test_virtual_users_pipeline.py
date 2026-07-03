import json
import logging
import threading

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.virtual_users.glm_generator import RuleBasedVirtualUserGenerator
from autoresearch.virtual_users.persona_source import build_fixture_persona_records
from autoresearch.virtual_users.pipeline import generate_virtual_user_batch
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    GenerationRequest,
    SourcePersona,
    VirtualUser,
    age_bucket_for_age,
)


class SparseAffinityGenerator:
    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        if persona.sex == "male":
            categories = ["Gaming"]
            category_affinity = {"Gaming": 0.91}
        else:
            categories = ["Music"]
            category_affinity = {"Music": 0.84}

        return VirtualUser(
            virtual_user_id=virtual_user_id,
            source_uuid=persona.uuid,
            age=persona.age,
            sex=persona.sex,
            age_bucket=age_bucket_for_age(persona.age),
            occupation=persona.occupation,
            province=persona.province,
            district=persona.district,
            country="KR",
            locale="ko-KR",
            persona_summary=persona.persona,
            hobby_keywords=["fixture"],
            interest_keywords=["fixture"],
            category_affinity=category_affinity,
            youtube_profile={
                "primary_categories": categories,
                "shorts_affinity": 0.5,
                "longform_affinity": 0.5,
                "trend_sensitivity": 0.5,
                "comment_propensity": 0.5,
                "watch_time_band": "mixed",
            },
            generation_meta={
                "schema_version": GENERATION_SCHEMA_VERSION,
                "prompt_version": PROMPT_VERSION,
                "llm_model": "sparse-fixture",
                "generated_at": "2026-07-02T00:00:00Z",
            },
        )


class ConcurrentProbeGenerator:
    def __init__(self) -> None:
        self.active = 0
        self.max_active = 0
        self.lock = threading.Lock()
        self.overlapped = threading.Event()

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        with self.lock:
            self.active += 1
            self.max_active = max(self.max_active, self.active)
            if self.active >= 2:
                self.overlapped.set()

        self.overlapped.wait(timeout=0.5)
        try:
            return VirtualUser(
                virtual_user_id=virtual_user_id,
                source_uuid=persona.uuid,
                age=persona.age,
                sex=persona.sex,
                age_bucket=age_bucket_for_age(persona.age),
                occupation=persona.occupation,
                province=persona.province,
                district=persona.district,
                country="KR",
                locale="ko-KR",
                persona_summary=persona.persona,
                hobby_keywords=["fixture"],
                interest_keywords=["fixture"],
                category_affinity={"Gaming": 0.5},
                youtube_profile={
                    "primary_categories": ["Gaming"],
                    "shorts_affinity": 0.5,
                    "longform_affinity": 0.5,
                    "trend_sensitivity": 0.5,
                    "comment_propensity": 0.5,
                    "watch_time_band": "mixed",
                },
                generation_meta={
                    "schema_version": GENERATION_SCHEMA_VERSION,
                    "prompt_version": PROMPT_VERSION,
                    "llm_model": "concurrent-fixture",
                    "generated_at": "2026-07-02T00:00:00Z",
                },
            )
        finally:
            with self.lock:
                self.active -= 1


def _affinity_map_to_dict(value: object) -> dict[str, float]:
    return {key: score for key, score in value}


def test_generate_virtual_user_batch_writes_expected_100_user_parquet(tmp_path, caplog):
    records = build_fixture_persona_records(male_count=60, female_count=60)
    output_path = tmp_path / "virtual_users_20s_100.parquet"
    warehouse_output_path = tmp_path / "virtual_users_kr.jsonl"
    request = GenerationRequest(
        male_count=50,
        female_count=50,
        seed=11,
        use_llm=False,
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
    rows = pq.read_table(output_path).to_pylist()
    assert len(rows) == 100
    assert sum(1 for row in rows if row["sex"] == "male") == 50
    assert sum(1 for row in rows if row["sex"] == "female") == 50
    assert rows[0]["source_dataset"] == "nvidia/Nemotron-Personas-Korea"
    assert rows[0]["district"]
    assert rows[0]["country"] == "KR"
    assert rows[0]["locale"] == "ko-KR"
    assert rows[0]["hobby_keywords"]
    assert rows[0]["interest_keywords"]
    assert rows[0]["category_affinity"]
    assert rows[0]["youtube_primary_categories"]
    assert batch.summary["male"] == 50
    assert batch.summary["female"] == 50
    assert "Starting virtual user batch generation" in caplog.text
    assert "Wrote virtual user batch parquet output" in caplog.text


def test_generate_virtual_user_batch_writes_affinity_as_map_not_sparse_struct(tmp_path):
    records = build_fixture_persona_records(male_count=1, female_count=1)
    output_path = tmp_path / "users.parquet"
    request = GenerationRequest(
        male_count=1,
        female_count=1,
        seed=17,
        use_llm=False,
        source_mode="fixture",
        output_path=str(output_path),
    )

    generate_virtual_user_batch(
        request=request,
        records=records,
        generator=SparseAffinityGenerator(),
    )

    table = pq.read_table(output_path)
    assert table.schema.field("category_affinity").type == pa.map_(
        pa.string(),
        pa.float64(),
    )
    rows = table.to_pylist()
    affinities = [_affinity_map_to_dict(row["category_affinity"]) for row in rows]
    assert {"Gaming": 0.91} in affinities
    assert {"Music": 0.84} in affinities
    assert all(None not in affinity.values() for affinity in affinities)


def test_generate_virtual_user_batch_can_generate_users_concurrently(tmp_path):
    records = build_fixture_persona_records(male_count=2, female_count=0)
    request = GenerationRequest(
        male_count=2,
        female_count=0,
        seed=17,
        use_llm=False,
        source_mode="fixture",
        max_concurrency=2,
        output_path=str(tmp_path / "users.parquet"),
    )
    generator = ConcurrentProbeGenerator()

    batch = generate_virtual_user_batch(
        request=request,
        records=records,
        generator=generator,
    )

    assert generator.max_active >= 2
    assert [user.virtual_user_id for user in batch.users] == ["vu_0001", "vu_0002"]


def test_generate_virtual_user_batch_uses_stable_virtual_user_ids(tmp_path):
    records = build_fixture_persona_records(male_count=10, female_count=10)
    request = GenerationRequest(
        male_count=2,
        female_count=2,
        seed=3,
        use_llm=False,
        source_mode="fixture",
        output_path=str(tmp_path / "users.parquet"),
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


def test_generate_virtual_user_batch_preserves_request_metadata_in_parquet(tmp_path):
    records = build_fixture_persona_records(male_count=5, female_count=5)
    output_path = tmp_path / "users.parquet"
    warehouse_output_path = tmp_path / "warehouse_users.jsonl"
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
    records = build_fixture_persona_records(male_count=5, female_count=5)
    batch_output_path = tmp_path / "virtual_users_batch.parquet"
    warehouse_output_path = tmp_path / "virtual_users_kr.jsonl"
    request = GenerationRequest(
        male_count=1,
        female_count=1,
        seed=7,
        use_llm=False,
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
    assert isinstance(rows[0]["hobby_keywords"], list)
    assert isinstance(rows[0]["interest_keywords"], list)
    assert isinstance(rows[0]["category_affinity"], dict)
    assert "primary_categories" in rows[0]
    assert "watch_time_band" in rows[0]
