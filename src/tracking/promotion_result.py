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
from typing import Literal, Self

from pydantic import BaseModel, ConfigDict, PrivateAttr, model_validator

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
    MANIFEST_ARTIFACT_INVALID = "manifest_artifact_invalid"
    SERVING_CALIBRATION_NOT_READY = "serving_calibration_not_ready"
    REGISTRY_EMPTY = "registry_empty"
    ALREADY_CHAMPION = "already_champion"
    EXPERIMENT_MODEL = "experiment_model"
    REGISTRY_ACCESS_FAILED = "registry_access_failed"
    METRIC_MISSING = "metric_missing"
    ARTIFACT_LOOKUP_FAILED = "artifact_lookup_failed"
    ALIAS_UPDATE_FAILED = "alias_update_failed"
    RESULT_WRITE_FAILED = "result_write_failed"
    UNEXPECTED_ERROR = "unexpected_error"


class ModelPromotionResult(BaseModel):
    """`promote-model`과 Airflow 사이의 v1 구조화 결과."""

    model_config = ConfigDict(allow_inf_nan=False)
    _legacy_message: str | None = PrivateAttr(default=None)

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

    @property
    def legacy_message(self) -> str | None:
        """구조화 JSON에는 포함하지 않는 legacy CLI 진단 문구."""
        return self._legacy_message

    def with_legacy_message(self, message: str) -> Self:
        """legacy 호출부에만 사용할 종전 진단 문맥을 결과에 연결한다."""
        self._legacy_message = message
        return self

    @model_validator(mode="after")
    def validate_outcome_reason(self) -> Self:
        """outcome마다 발생 가능한 안정 reason code만 허용한다."""
        allowed_reasons = {
            PromotionOutcome.PROMOTED: {
                PromotionReasonCode.FIRST_CHAMPION,
                PromotionReasonCode.METRIC_NOT_DEGRADED,
            },
            PromotionOutcome.REJECTED: {
                PromotionReasonCode.METRIC_BELOW_CHAMPION,
                PromotionReasonCode.CALIBRATION_ARTIFACT_MISSING,
                PromotionReasonCode.MANIFEST_ARTIFACT_INVALID,
                PromotionReasonCode.SERVING_CALIBRATION_NOT_READY,
            },
            PromotionOutcome.NO_CANDIDATE: {
                PromotionReasonCode.REGISTRY_EMPTY,
                PromotionReasonCode.ALREADY_CHAMPION,
                # 등록된 버전이 전부 실험 모델이라 승격 가능한 후보가 없다(#405).
                # REJECTED가 아니라 NO_CANDIDATE인 이유: 게이트를 못 넘은 게 아니라
                # 애초에 심사 대상이 없는 상태라, 일일 DAG의 알람 해석이 어긋나지 않는다.
                PromotionReasonCode.EXPERIMENT_MODEL,
            },
            PromotionOutcome.ERROR: {
                PromotionReasonCode.REGISTRY_ACCESS_FAILED,
                PromotionReasonCode.METRIC_MISSING,
                PromotionReasonCode.ARTIFACT_LOOKUP_FAILED,
                PromotionReasonCode.ALIAS_UPDATE_FAILED,
                PromotionReasonCode.RESULT_WRITE_FAILED,
                PromotionReasonCode.UNEXPECTED_ERROR,
            },
        }
        if self.reason_code not in allowed_reasons[self.outcome]:
            raise ValueError(
                f"reason_code={self.reason_code.value} is invalid for "
                f"outcome={self.outcome.value}"
            )
        return self


class PromotionExecutionError(RuntimeError):
    """안전한 reason code를 보존하는 모델 승격 실행 오류."""

    def __init__(
        self,
        reason_code: PromotionReasonCode,
        message: str,
        *,
        candidate_version: str | None = None,
        champion_version: str | None = None,
        candidate_metric: float | None = None,
        champion_metric: float | None = None,
    ) -> None:
        super().__init__(message)
        self.reason_code = reason_code
        self.candidate_version = candidate_version
        self.champion_version = champion_version
        self.candidate_metric = candidate_metric
        self.champion_metric = champion_metric


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
