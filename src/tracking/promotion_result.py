"""모델 승격 판정의 구조화 결과 계약.

[파이프라인] CTR 학습·평가 뒤 Model Registry의 champion alias를 갱신하는
판정 구간에서, 판정 본체와 Airflow 실행 경계 사이에 전달할 결과를 정의한다.

[기능] 승격 outcome과 reason code를 타입으로 제한하고
`model-promotion-result-v1` JSON schema를 제공한다.

[비책임] MLflow 게이트 판정과 alias 이동(src/tracking/promote.py), 결과 파일
운반과 Slack 전송(Autoresearch-airflow)은 담당하지 않는다.
"""

from __future__ import annotations

import os
from enum import Enum
from pathlib import Path
from tempfile import NamedTemporaryFile
from typing import Literal

from pydantic import BaseModel

MODEL_PROMOTION_RESULT_CONTRACT = "model-promotion-result-v1"


class PromotionOutcome(str, Enum):
    """모델 승격 실행의 최종 분류."""

    PROMOTED = "promoted"
    REJECTED = "rejected"
    NO_CANDIDATE = "no_candidate"
    ERROR = "error"


class PromotionReasonCode(str, Enum):
    """소비자가 문자열 로그 파싱 없이 해석하는 안정된 판정 사유."""

    FIRST_CHAMPION = "first_champion"
    METRIC_NOT_DEGRADED = "metric_not_degraded"
    METRIC_BELOW_CHAMPION = "metric_below_champion"
    CALIBRATION_ARTIFACT_MISSING = "calibration_artifact_missing"
    SERVING_CALIBRATION_NOT_READY = "serving_calibration_not_ready"
    REGISTRY_EMPTY = "registry_empty"
    ALREADY_CHAMPION = "already_champion"
    REGISTRY_ACCESS_FAILED = "registry_access_failed"
    METRIC_MISSING = "metric_missing"
    ARTIFACT_LOOKUP_FAILED = "artifact_lookup_failed"
    ALIAS_UPDATE_FAILED = "alias_update_failed"
    RESULT_WRITE_FAILED = "result_write_failed"
    UNEXPECTED_ERROR = "unexpected_error"


class ModelPromotionResult(BaseModel):
    """`promote-model`과 Airflow 사이의 v1 구조화 결과."""

    event: Literal["model_promotion_result"] = "model_promotion_result"
    contract_version: Literal["model-promotion-result-v1"] = (
        "model-promotion-result-v1"
    )
    outcome: PromotionOutcome
    model_name: str
    champion_alias: str
    candidate_version: str | None = None
    champion_version: str | None = None
    metric_name: Literal["val_roc_auc"] = "val_roc_auc"
    candidate_metric: float | None = None
    champion_metric: float | None = None
    reason_code: PromotionReasonCode


class PromotionExecutionError(RuntimeError):
    """안전한 reason code를 보존하는 모델 승격 실행 오류."""

    def __init__(
        self,
        reason_code: PromotionReasonCode,
        message: str,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code


def write_result_file(result: ModelPromotionResult, path: Path) -> None:
    """구조화 결과를 같은 디렉토리의 임시 파일을 거쳐 원자적으로 교체한다."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path: Path | None = None
    try:
        with NamedTemporaryFile(
            mode="w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary_file:
            temporary_path = Path(temporary_file.name)
            temporary_file.write(result.model_dump_json())
            temporary_file.flush()
            os.fsync(temporary_file.fileno())

        os.replace(temporary_path, path)
        temporary_path = None
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)
