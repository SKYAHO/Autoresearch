"""정규화된 persona 목록에서 virtual user batch와 warehouse row를 생성한다."""

import json
import logging
from pathlib import Path
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq
from pydantic import ValidationError

from autoresearch.virtual_users.glm_generator import assemble_virtual_user
from autoresearch.virtual_users.persona_source import sample_raw_personas_by_contract
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_DATASET,
    GenerationRequest,
    GenerationResult,
    QuarantineRecord,
    VirtualUser,
    VirtualUserBatch,
)


logger = logging.getLogger(__name__)


class VirtualUserGenerator(Protocol):
    """pipeline이 provider 구현을 같은 방식으로 호출하기 위한 인터페이스."""

    model_name: str

    def generate(self, raw_row: dict, virtual_user_id: str) -> str:
        """raw persona dict 한 건에 대한 raw LLM 응답 text를 반환한다."""

        ...


VIRTUAL_USERS_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("user_id", pa.string()),
        pa.field("source_uuid", pa.string()),
        pa.field("source_dataset", pa.string()),
        pa.field("source_hash", pa.string()),
        pa.field("batch_schema_version", pa.string()),
        pa.field("batch_prompt_version", pa.string()),
        pa.field("batch_generated_at", pa.string()),
        pa.field("request_age_min", pa.int64()),
        pa.field("request_age_max", pa.int64()),
        pa.field("request_male_count", pa.int64()),
        pa.field("request_female_count", pa.int64()),
        pa.field("request_seed", pa.int64()),
        pa.field("request_max_concurrency", pa.int64()),
        pa.field("request_use_llm", pa.bool_()),
        pa.field("request_source_mode", pa.string()),
        pa.field("age", pa.int64()),
        pa.field("sex", pa.string()),
        pa.field("age_bucket", pa.string()),
        pa.field("marital_status", pa.string()),
        pa.field("military_status", pa.string()),
        pa.field("family_type", pa.string()),
        pa.field("housing_type", pa.string()),
        pa.field("education_level", pa.string()),
        pa.field("bachelors_field", pa.string()),
        pa.field("occupation", pa.string()),
        pa.field("province", pa.string()),
        pa.field("district", pa.string()),
        pa.field("country", pa.string()),
        pa.field("locale", pa.string()),
        pa.field("persona_summary", pa.string()),
        pa.field("hobby_keywords", pa.list_(pa.string())),
        pa.field("interest_keywords", pa.list_(pa.string())),
        pa.field("lifestyle_keywords", pa.list_(pa.string())),
        pa.field("food_keywords", pa.list_(pa.string())),
        pa.field("travel_keywords", pa.list_(pa.string())),
        pa.field("career_keywords", pa.list_(pa.string())),
        pa.field("family_context_keywords", pa.list_(pa.string())),
        pa.field("primary_categories", pa.list_(pa.string())),
        pa.field("trend_sensitivity", pa.float64()),
        pa.field("comment_propensity", pa.float64()),
        pa.field("watch_time_band", pa.string()),
        pa.field("source_persona_json", pa.string()),
        pa.field("generation_schema_version", pa.string()),
        pa.field("generation_prompt_version", pa.string()),
        pa.field("llm_model", pa.string()),
        pa.field("generated_at", pa.string()),
    ]
)


def _json_string(value: object) -> str:
    """Nested evidence/source payload를 DuckDB-friendly JSON string으로 저장한다."""

    return json.dumps(value, ensure_ascii=False, default=str, sort_keys=True)


def _virtual_user_rows(batch: VirtualUserBatch) -> list[dict[str, object]]:
    """VirtualUserBatch를 명시적 Parquet schema에 맞는 flat row로 변환한다."""

    rows: list[dict[str, object]] = []
    for user in batch.users:
        rows.append(
            {
                "user_id": user.virtual_user_id,
                "source_uuid": user.source_uuid,
                "source_dataset": batch.source_dataset,
                "source_hash": user.source_hash,
                "batch_schema_version": batch.schema_version,
                "batch_prompt_version": batch.prompt_version,
                "batch_generated_at": batch.generated_at,
                "request_age_min": batch.request.age_min,
                "request_age_max": batch.request.age_max,
                "request_male_count": batch.request.male_count,
                "request_female_count": batch.request.female_count,
                "request_seed": batch.request.seed,
                "request_max_concurrency": batch.request.max_concurrency,
                "request_use_llm": batch.request.use_llm,
                "request_source_mode": batch.request.source_mode,
                "age": user.age,
                "sex": user.sex,
                "age_bucket": user.age_bucket,
                "marital_status": user.marital_status,
                "military_status": user.military_status,
                "family_type": user.family_type,
                "housing_type": user.housing_type,
                "education_level": user.education_level,
                "bachelors_field": user.bachelors_field,
                "occupation": user.occupation,
                "province": user.province,
                "district": user.district,
                "country": user.country,
                "locale": user.locale,
                "persona_summary": user.persona_summary,
                "hobby_keywords": user.hobby_keywords,
                "interest_keywords": user.interest_keywords,
                "lifestyle_keywords": user.lifestyle_keywords,
                "food_keywords": user.food_keywords,
                "travel_keywords": user.travel_keywords,
                "career_keywords": user.career_keywords,
                "family_context_keywords": user.family_context_keywords,
                "primary_categories": user.youtube_profile.primary_categories,
                "trend_sensitivity": user.youtube_profile.trend_sensitivity,
                "comment_propensity": user.youtube_profile.comment_propensity,
                "watch_time_band": user.youtube_profile.watch_time_band,
                "source_persona_json": _json_string(user.source_persona_json),
                "generation_schema_version": user.generation_meta.schema_version,
                "generation_prompt_version": user.generation_meta.prompt_version,
                "llm_model": user.generation_meta.llm_model,
                "generated_at": user.generation_meta.generated_at,
            }
        )
    return rows


def _write_virtual_users_parquet(batch: VirtualUserBatch, output_path: Path) -> None:
    """VirtualUserBatch를 명시적 Arrow schema의 Parquet 파일로 저장한다."""

    table = pa.Table.from_pylist(
        _virtual_user_rows(batch),
        schema=VIRTUAL_USERS_PARQUET_SCHEMA,
    )
    pq.write_table(table, output_path)


def write_quarantine_jsonl(
    records: list[QuarantineRecord],
    output_path: str | Path,
) -> None:
    """생성 실패로 격리된 행을 후처리를 위한 JSONL 파일로 저장한다."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(
                json.dumps(record.model_dump(), ensure_ascii=False, default=str) + "\n"
            )
    logger.info(
        "Wrote quarantine output",
        extra={"output_path": str(path), "total": len(records)},
    )


def _generate_isolated(
    generator: VirtualUserGenerator,
    records: list[dict],
) -> tuple[list[VirtualUser], list[QuarantineRecord]]:
    """행 단위로 생성을 격리해 한 행의 실패가 배치 전체를 중단시키지 않게 한다."""

    users: list[VirtualUser] = []
    quarantine: list[QuarantineRecord] = []
    for index, raw_row in enumerate(records, start=1):
        virtual_user_id = f"vu_{index:04d}"
        source_uuid = str(raw_row.get("uuid", ""))
        try:
            raw_text = generator.generate(raw_row, virtual_user_id)
        except Exception as exc:  # noqa: BLE001 - API/transport failure isolation
            quarantine.append(
                QuarantineRecord(
                    source_uuid=source_uuid,
                    raw_row=raw_row,
                    raw_llm_response="",
                    error_type="api_error",
                    error_message=str(exc),
                )
            )
            continue
        try:
            users.append(
                assemble_virtual_user(raw_row, raw_text, virtual_user_id, generator.model_name)
            )
        except json.JSONDecodeError as exc:
            quarantine.append(
                QuarantineRecord(
                    source_uuid=source_uuid,
                    raw_row=raw_row,
                    raw_llm_response=raw_text,
                    error_type="invalid_json",
                    error_message=str(exc),
                )
            )
        except (ValidationError, ValueError, KeyError, TypeError, AttributeError) as exc:
            quarantine.append(
                QuarantineRecord(
                    source_uuid=source_uuid,
                    raw_row=raw_row,
                    raw_llm_response=raw_text,
                    error_type="schema_fail",
                    error_message=str(exc),
                )
            )
    return users, quarantine


class BatchGenerationError(RuntimeError):
    """배치의 격리 비율이 임계치를 넘어 전량/대량 실패로 판정될 때 발생한다."""


def generate_virtual_user_batch(
    request: GenerationRequest,
    records: list[dict],
    generator: VirtualUserGenerator,
) -> GenerationResult:
    """persona 샘플링, 행 단위 격리 생성, batch/warehouse/quarantine 파일 저장을 실행한다."""

    logger.info(
        "Starting virtual user batch generation",
        extra={
            "age_min": request.age_min,
            "age_max": request.age_max,
            "male_count": request.male_count,
            "female_count": request.female_count,
            "seed": request.seed,
            "max_concurrency": request.max_concurrency,
            "source_mode": request.source_mode,
            "use_llm": request.use_llm,
        },
    )

    sampled = sample_raw_personas_by_contract(
        records=records,
        age_min=request.age_min,
        age_max=request.age_max,
        male_count=request.male_count,
        female_count=request.female_count,
        seed=request.seed,
    )
    logger.info(
        "Sampled personas for batch generation",
        extra={"sampled_count": len(sampled)},
    )

    users, quarantine = _generate_isolated(generator, sampled)

    batch = VirtualUserBatch(
        schema_version=GENERATION_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        source_dataset=SOURCE_DATASET,
        request=request,
        users=users,
    )
    result = GenerationResult(batch=batch, quarantine=quarantine)
    logger.info("Generated virtual user batch", extra=result.summary)

    # 전량/대량 실패 가드: 행 단위 격리(한 행이 배치를 못 죽임)와 별개로, 배치 전체가
    # 조용히 빈/부실 결과로 "성공 종료"하는 상황을 막는다. 격리 파일은 포렌식용으로 남기고
    # 실패로 종료해 운영자가 "전량 실패"와 "정상 실행"을 구분할 수 있게 한다.
    if sampled:
        quarantine_ratio = len(quarantine) / len(sampled)
        if quarantine_ratio > request.max_quarantine_ratio:
            write_quarantine_jsonl(quarantine, request.quarantine_output_path)
            raise BatchGenerationError(
                f"quarantine ratio {quarantine_ratio:.2f} exceeds max_quarantine_ratio "
                f"{request.max_quarantine_ratio:.2f} "
                f"(valid={len(users)}, quarantined={len(quarantine)}, sampled={len(sampled)})"
            )

    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_virtual_users_parquet(batch, output_path)
    logger.info(
        "Wrote virtual user batch parquet output",
        extra={
            "output_path": str(output_path),
            "total": batch.summary["total"],
            "male": batch.summary["male"],
            "female": batch.summary["female"],
        },
    )
    write_quarantine_jsonl(quarantine, request.quarantine_output_path)
    return result
