"""Hugging Face persona raw data를 가져와 내부 SourcePersona로 정규화한다."""

import json
import logging
import random
from collections.abc import Iterable
from pathlib import Path
from typing import Any

from datasets import load_dataset

from autoresearch.virtual_users.schema import SOURCE_DATASET, SourcePersona


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


def _as_text(record: dict[str, Any], key: str) -> str:
    """raw record의 optional 값을 빈 문자열 또는 문자열로 안전하게 읽는다."""

    value = record.get(key, "")
    if value is None:
        return ""
    return str(value)


def _as_text_list(record: dict[str, Any], key: str) -> list[str]:
    """raw record의 list형/문자열형 관심사 필드를 문자열 list로 맞춘다."""

    value = record.get(key, [])
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item) for item in value if str(item).strip()]
    return [part.strip() for part in str(value).split(",") if part.strip()]


def source_persona_from_record(record: dict[str, Any]) -> SourcePersona:
    """Hugging Face raw row 하나를 검증 가능한 SourcePersona 한 건으로 변환한다."""

    persona = SourcePersona(
        uuid=_as_text(record, "uuid"),
        age=int(record["age"]),
        sex=normalize_sex(record["sex"]),
        occupation=_as_text(record, "occupation"),
        province=_as_text(record, "province"),
        district=_as_text(record, "district"),
        persona=_as_text(record, "persona"),
        hobbies_and_interests=_as_text(record, "hobbies_and_interests"),
        hobbies_and_interests_list=_as_text_list(record, "hobbies_and_interests_list"),
        professional_persona=_as_text(record, "professional_persona"),
        skills_and_expertise=_as_text(record, "skills_and_expertise"),
        sports_persona=_as_text(record, "sports_persona"),
        arts_persona=_as_text(record, "arts_persona"),
        travel_persona=_as_text(record, "travel_persona"),
        culinary_persona=_as_text(record, "culinary_persona"),
        family_persona=_as_text(record, "family_persona"),
        cultural_background=_as_text(record, "cultural_background"),
    )
    logger.debug(
        "Converted raw persona record",
        extra={
            "source_uuid": persona.uuid,
            "age": persona.age,
            "sex": persona.sex,
            "province": persona.province,
            "country": persona.country,
            "locale": persona.locale,
        },
    )
    return persona


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


def load_nvidia_persona_records(
    max_records: int | None = None,
    raw_output_path: str | Path | None = None,
) -> list[SourcePersona]:
    """NVIDIA Persona dataset을 streaming으로 읽고 유효한 persona만 반환한다."""

    logger.info(
        "Loading NVIDIA persona records",
        extra={"source_dataset": SOURCE_DATASET, "max_records": max_records},
    )
    dataset = load_dataset(SOURCE_DATASET, split="train", streaming=True)
    raw_records: list[dict[str, Any]] = []
    records: list[SourcePersona] = []
    skipped = 0

    for raw_record in dataset:
        raw_payload = dict(raw_record)
        raw_records.append(raw_payload)
        try:
            records.append(source_persona_from_record(raw_payload))
        except (KeyError, TypeError, ValueError):
            skipped += 1
            logger.debug("Skipped invalid persona record", exc_info=True)
            continue
        if max_records is not None and len(records) >= max_records:
            break

    if raw_output_path is not None:
        write_raw_persona_records(raw_records, raw_output_path)

    logger.info(
        "Loaded NVIDIA persona records",
        extra={
            "source_dataset": SOURCE_DATASET,
            "loaded_count": len(records),
            "skipped_count": skipped,
        },
    )
    return records


def build_fixture_persona_records(
    male_count: int = 60,
    female_count: int = 60,
) -> list[SourcePersona]:
    """외부 dataset/Gemini 없이 테스트할 수 있는 deterministic fixture를 만든다."""

    rows: list[SourcePersona] = []
    for index in range(male_count):
        age = 20 + (index % 10)
        rows.append(
            SourcePersona(
                uuid=f"fixture-m-{index:03d}",
                age=age,
                sex="male",
                occupation="student" if index % 2 == 0 else "office worker",
                province="Seoul",
                district="Mapo-gu",
                persona="A 20s male persona interested in gaming, music, and creators.",
                hobbies_and_interests="gaming, music, short-form video",
                professional_persona="Early career learner.",
                sports_persona="Occasional sports highlights viewer.",
                arts_persona="Interested in popular music.",
                cultural_background="Korean urban digital media user.",
            )
        )
    for index in range(female_count):
        age = 20 + (index % 10)
        rows.append(
            SourcePersona(
                uuid=f"fixture-f-{index:03d}",
                age=age,
                sex="female",
                occupation="student" if index % 2 == 0 else "designer",
                province="Gyeonggi-do",
                district="Seongnam-si",
                persona="A 20s female persona interested in music, lifestyle, and learning.",
                hobbies_and_interests="music, beauty, lifestyle, study video",
                professional_persona="Early career planner.",
                sports_persona="Light sports content viewer.",
                arts_persona="Interested in music and visual culture.",
                cultural_background="Korean mobile-first media user.",
            )
        )

    logger.debug(
        "Built fixture persona records",
        extra={"male_count": male_count, "female_count": female_count, "total": len(rows)},
    )
    return rows


def sample_personas_by_contract(
    records: Iterable[SourcePersona],
    age_min: int,
    age_max: int,
    male_count: int,
    female_count: int,
    seed: int,
) -> list[SourcePersona]:
    """요청한 나이/성별 조건에 맞는 persona를 seed 기반으로 재현 가능하게 샘플링한다."""

    eligible = [record for record in records if age_min <= record.age <= age_max]
    male_records = [record for record in eligible if record.sex == "male"]
    female_records = [record for record in eligible if record.sex == "female"]

    logger.info(
        "Filtered source personas for virtual user sampling",
        extra={
            "age_min": age_min,
            "age_max": age_max,
            "eligible_count": len(eligible),
            "available_male_count": len(male_records),
            "available_female_count": len(female_records),
            "requested_male_count": male_count,
            "requested_female_count": female_count,
            "seed": seed,
        },
    )

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
        "Sampled source personas for virtual user generation",
        extra={
            "sampled_total": len(sampled),
            "sampled_male_count": male_count,
            "sampled_female_count": female_count,
            "seed": seed,
        },
    )
    return sampled
