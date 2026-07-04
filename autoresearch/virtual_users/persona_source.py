"""Hugging Face persona raw data를 가져와 내부 raw dict 계약으로 정규화한다."""

import json
import logging
import random
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from datasets import load_dataset

from autoresearch.virtual_users.schema import SOURCE_DATASET


logger = logging.getLogger(__name__)

MALE_VALUES = {"male", "m", "man", "남성", "남자"}
FEMALE_VALUES = {"female", "f", "woman", "여성", "여자"}


def normalize_sex(value: object) -> str:
    """원천 데이터의 성별 값을 pipeline 표준값인 male/female로 변환한다."""

    normalized = str(value).strip().lower()
    if normalized in MALE_VALUES:
        return "male"
    if normalized in FEMALE_VALUES:
        return "female"
    logger.debug("Unsupported source persona sex value", extra={"raw_sex": str(value)})
    raise ValueError(f"Unsupported sex value: {value}")


def record_age(record: dict[str, Any]) -> int | None:
    """raw record의 age를 int로 읽되, 불가하면 None."""
    try:
        return int(record["age"])
    except (KeyError, TypeError, ValueError):
        return None


def record_sex(record: dict[str, Any]) -> str | None:
    """raw record의 sex를 male/female로 읽되, 불가하면 None."""
    try:
        return normalize_sex(record["sex"])
    except (KeyError, ValueError):
        return None


def sample_raw_personas_by_contract(
    records: list[dict[str, Any]],
    age_min: int,
    age_max: int,
    male_count: int,
    female_count: int,
    seed: int,
) -> list[dict[str, Any]]:
    """raw dict에서 연령/성별을 읽어 seed 기반 균형 샘플을 만든다."""
    eligible = [
        record
        for record in records
        if (age := record_age(record)) is not None and age_min <= age <= age_max
    ]
    male_records = [r for r in eligible if record_sex(r) == "male"]
    female_records = [r for r in eligible if record_sex(r) == "female"]

    if len(male_records) < male_count:
        raise ValueError(
            f"Not enough male personas: requested={male_count}, available={len(male_records)}"
        )
    if len(female_records) < female_count:
        raise ValueError(
            f"Not enough female personas: requested={female_count}, "
            f"available={len(female_records)}"
        )

    rng = random.Random(seed)
    male_pool = list(male_records)
    female_pool = list(female_records)
    rng.shuffle(male_pool)
    rng.shuffle(female_pool)
    sampled = male_pool[:male_count] + female_pool[:female_count]
    rng.shuffle(sampled)

    logger.info(
        "Sampled raw personas for virtual user generation",
        extra={
            "sampled_total": len(sampled),
            "sampled_male_count": male_count,
            "sampled_female_count": female_count,
            "seed": seed,
        },
    )
    return sampled


def write_raw_persona_records(
    records: Iterable[dict[str, Any]],
    output_path: str | Path,
) -> None:
    """재현성과 live QA 확인을 위해 raw persona payload를 JSONL로 저장한다."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record, ensure_ascii=False, default=str) + "\n")
    logger.info("Wrote raw persona snapshot", extra={"output_path": str(path)})


def build_fixture_raw_persona_records(
    male_count: int = 60,
    female_count: int = 60,
) -> list[dict[str, Any]]:
    """외부 dataset/LLM 없이 테스트할 수 있는 deterministic raw dict fixture."""
    rows: list[dict[str, Any]] = []
    for index in range(male_count):
        rows.append(
            {
                "uuid": f"fixture-m-{index:03d}",
                "age": 20 + (index % 10),
                "sex": "남자",
                "occupation": "student" if index % 2 == 0 else "office worker",
                "province": "서울",
                "district": "마포구",
                "persona": "게임과 음악을 즐기는 20대 남성.",
                "hobbies_and_interests": "게임, 음악, 숏폼",
            }
        )
    for index in range(female_count):
        rows.append(
            {
                "uuid": f"fixture-f-{index:03d}",
                "age": 20 + (index % 10),
                "sex": "여자",
                "occupation": "student" if index % 2 == 0 else "designer",
                "province": "경기",
                "district": "성남시",
                "persona": "음악과 라이프스타일을 즐기는 20대 여성.",
                "hobbies_and_interests": "음악, 뷰티, 라이프스타일",
            }
        )
    return rows


def load_raw_persona_records(
    max_records: int | None = None,
    raw_output_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """NVIDIA Persona dataset을 streaming으로 읽어 raw dict 그대로 반환한다."""
    logger.info(
        "Loading raw NVIDIA persona records",
        extra={"source_dataset": SOURCE_DATASET, "max_records": max_records},
    )
    dataset = load_dataset(SOURCE_DATASET, split="train", streaming=True)
    records: list[dict[str, Any]] = []
    for raw_record in dataset:
        records.append(dict(raw_record))
        if max_records is not None and len(records) >= max_records:
            break

    if raw_output_path is not None:
        write_raw_persona_records(records, raw_output_path)

    logger.info(
        "Loaded raw NVIDIA persona records",
        extra={"source_dataset": SOURCE_DATASET, "loaded_count": len(records)},
    )
    return records
