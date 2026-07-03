"""정규화된 persona 목록에서 virtual user batch와 warehouse row를 생성한다."""

import json
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Protocol

import pyarrow as pa
import pyarrow.parquet as pq

from autoresearch.virtual_users.persona_source import sample_personas_by_contract
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_DATASET,
    GenerationRequest,
    SourcePersona,
    VirtualUser,
    VirtualUserBatch,
)


logger = logging.getLogger(__name__)


class VirtualUserGenerator(Protocol):
    """pipeline이 provider 구현을 같은 방식으로 호출하기 위한 인터페이스."""

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        """SourcePersona 한 건을 VirtualUser 한 건으로 변환한다."""

        ...


VIRTUAL_USERS_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("virtual_user_id", pa.string()),
        pa.field("source_uuid", pa.string()),
        pa.field("source_dataset", pa.string()),
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
        pa.field("request_use_gemini", pa.bool_()),
        pa.field("request_source_mode", pa.string()),
        pa.field("age", pa.int64()),
        pa.field("sex", pa.string()),
        pa.field("age_bucket", pa.string()),
        pa.field("occupation", pa.string()),
        pa.field("province", pa.string()),
        pa.field("district", pa.string()),
        pa.field("country", pa.string()),
        pa.field("locale", pa.string()),
        pa.field("persona_summary", pa.string()),
        pa.field("hobby_keywords", pa.list_(pa.string())),
        pa.field("interest_keywords", pa.list_(pa.string())),
        pa.field("category_affinity", pa.map_(pa.string(), pa.float64())),
        pa.field("youtube_primary_categories", pa.list_(pa.string())),
        pa.field("shorts_affinity", pa.float64()),
        pa.field("longform_affinity", pa.float64()),
        pa.field("trend_sensitivity", pa.float64()),
        pa.field("comment_propensity", pa.float64()),
        pa.field("watch_time_band", pa.string()),
        pa.field("generation_schema_version", pa.string()),
        pa.field("generation_prompt_version", pa.string()),
        pa.field("llm_model", pa.string()),
        pa.field("generated_at", pa.string()),
    ]
)


def _map_entries(values: dict[str, float]) -> list[tuple[str, float]]:
    """PyArrow map<string, double> 저장을 위해 dict를 안정적인 pair list로 바꾼다."""

    return [(key, float(values[key])) for key in sorted(values)]


def _virtual_user_rows(batch: VirtualUserBatch) -> list[dict[str, object]]:
    """VirtualUserBatch를 명시적 Parquet schema에 맞는 flat row로 변환한다."""

    rows: list[dict[str, object]] = []
    for user in batch.users:
        rows.append(
            {
                "virtual_user_id": user.virtual_user_id,
                "source_uuid": user.source_uuid,
                "source_dataset": batch.source_dataset,
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
                "request_use_gemini": batch.request.use_gemini,
                "request_source_mode": batch.request.source_mode,
                "age": user.age,
                "sex": user.sex,
                "age_bucket": user.age_bucket,
                "occupation": user.occupation,
                "province": user.province,
                "district": user.district,
                "country": user.country,
                "locale": user.locale,
                "persona_summary": user.persona_summary,
                "hobby_keywords": user.hobby_keywords,
                "interest_keywords": user.interest_keywords,
                "category_affinity": _map_entries(user.category_affinity),
                "youtube_primary_categories": user.youtube_profile.primary_categories,
                "shorts_affinity": user.youtube_profile.shorts_affinity,
                "longform_affinity": user.youtube_profile.longform_affinity,
                "trend_sensitivity": user.youtube_profile.trend_sensitivity,
                "comment_propensity": user.youtube_profile.comment_propensity,
                "watch_time_band": user.youtube_profile.watch_time_band,
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


def write_virtual_users_warehouse_jsonl(
    batch: VirtualUserBatch,
    output_path: str | Path,
) -> None:
    """VirtualUserBatch를 Data Warehouse 적재 직전 JSONL row 파일로 저장한다."""

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for user in batch.users:
            file.write(
                json.dumps(user.to_warehouse_row(), ensure_ascii=False, default=str)
                + "\n"
            )
    logger.info(
        "Wrote warehouse-ready virtual user output",
        extra={"output_path": str(path), "total": len(batch.users)},
    )


def _generate_one_user(
    generator: VirtualUserGenerator,
    index: int,
    persona: SourcePersona,
) -> tuple[int, VirtualUser]:
    """virtual_user_id를 안정적으로 부여해 단일 user를 생성한다."""

    virtual_user_id = f"vu_{index:04d}"
    return index, generator.generate(persona, virtual_user_id=virtual_user_id)


def _generate_users(
    sampled: list[SourcePersona],
    generator: VirtualUserGenerator,
    max_concurrency: int,
) -> list[VirtualUser]:
    """요청된 concurrency로 virtual user를 생성하되 결과 순서를 보존한다."""

    if max_concurrency == 1 or len(sampled) <= 1:
        return [
            _generate_one_user(generator, index, persona)[1]
            for index, persona in enumerate(sampled, start=1)
        ]

    users_by_index: dict[int, VirtualUser] = {}
    with ThreadPoolExecutor(max_workers=max_concurrency) as executor:
        futures = [
            executor.submit(_generate_one_user, generator, index, persona)
            for index, persona in enumerate(sampled, start=1)
        ]
        for future in as_completed(futures):
            index, user = future.result()
            users_by_index[index] = user

    return [users_by_index[index] for index in range(1, len(sampled) + 1)]


def generate_virtual_user_batch(
    request: GenerationRequest,
    records: list[SourcePersona],
    generator: VirtualUserGenerator,
) -> VirtualUserBatch:
    """persona 샘플링, virtual user 생성, batch/warehouse 파일 저장을 실행한다."""

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
            "use_gemini": request.use_gemini,
        },
    )

    sampled = sample_personas_by_contract(
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

    users = _generate_users(
        sampled=sampled,
        generator=generator,
        max_concurrency=request.max_concurrency,
    )

    batch = VirtualUserBatch(
        schema_version=GENERATION_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        source_dataset=SOURCE_DATASET,
        request=request,
        users=users,
    )
    logger.info(
        "Generated virtual users",
        extra={
            "generated_total": batch.summary["total"],
            "generated_male": batch.summary["male"],
            "generated_female": batch.summary["female"],
        },
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
    write_virtual_users_warehouse_jsonl(
        batch=batch,
        output_path=request.warehouse_output_path,
    )
    return batch
