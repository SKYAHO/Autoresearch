"""정규화된 persona 목록에서 virtual user batch와 warehouse row를 생성한다."""

import json
import logging
from pathlib import Path

from autoresearch.virtual_users.gemini_generator import VirtualUserGenerator
from autoresearch.virtual_users.persona_source import sample_personas_by_contract
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_DATASET,
    GenerationRequest,
    SourcePersona,
    VirtualUserBatch,
)


logger = logging.getLogger(__name__)


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


def generate_virtual_user_batch(
    request: GenerationRequest,
    records: list[SourcePersona],
    generator: VirtualUserGenerator,
) -> VirtualUserBatch:
    """persona 샘플링, virtual user 생성, batch/warehouse 파일 저장을 순서대로 실행한다."""

    logger.info(
        "Starting virtual user batch generation",
        extra={
            "age_min": request.age_min,
            "age_max": request.age_max,
            "male_count": request.male_count,
            "female_count": request.female_count,
            "seed": request.seed,
            "source_mode": request.source_mode,
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

    users = []
    for index, persona in enumerate(sampled, start=1):
        virtual_user_id = f"vu_{index:04d}"
        users.append(generator.generate(persona, virtual_user_id=virtual_user_id))

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
    output_path.write_text(
        json.dumps(batch.to_output_dict(), ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    logger.info(
        "Wrote virtual user batch output",
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
