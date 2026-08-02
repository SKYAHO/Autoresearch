"""write-once 실험 승격 evidence의 application 저장소 adapter (#466).

[파이프라인] 학습 이전의 experiment plan과 학습 runtime이 계산한 held-out metric을
immutable GCS object로 식별해, training·comparison·evaluation 사이에서 신뢰할 수 있는
승격 근거를 전달하는 구간을 담당한다.

[기능] strict Pydantic receipt 계약, canonical JSON SHA-256, create-only publish,
generation-pinned re-read와 GCS metadata 재검증을 제공한다.

[비책임] IAM·retention·production prefix deny는 Autoresearch-infra #485가 집행하며,
모델 fit은 train.py, 공정 comparison은 training_comparison.py, 통계 verdict는
experiment_evaluation.py, registry alias 이동은 후속 #470이 담당한다.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Annotated, Literal
from urllib.parse import urlparse

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator


SHA256_PATTERN = r"^[0-9a-f]{64}$"
PROMOTION_POLICY_VERSION = "promotion-policy-v1"


class PromotionEvidenceValidationError(ValueError):
    """write-once promotion evidence가 검증되지 않을 때 발생한다."""


class _ImmutableModel(BaseModel):
    """promotion evidence 계약에 공통 적용하는 strict immutable 설정."""

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def _normalize_utc_datetime(value: datetime) -> datetime:
    """timezone-aware datetime을 UTC로 정규화하고 naive 시각을 거부한다."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError("timezone 정보를 포함한 UTC 시각이 필요합니다")
    return value.astimezone(timezone.utc)


def _json_default(value: object) -> str:
    """canonical JSON의 datetime 입력을 UTC ISO-8601 문자열로 바꾼다."""
    if isinstance(value, datetime):
        return _normalize_utc_datetime(value).isoformat()
    raise TypeError(f"canonical JSON으로 정규화할 수 없는 값입니다: {value!r}")


def _canonical_json_bytes(value: BaseModel | dict[str, object]) -> bytes:
    """계약 값의 hash·GCS body에 공통 사용할 canonical JSON byte를 만든다."""
    payload: object
    if isinstance(value, BaseModel):
        payload = value.model_dump(mode="json")
    else:
        payload = value
    return json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
        default=_json_default,
    ).encode("utf-8")


def _sha256(value: bytes) -> str:
    """byte 전체 SHA-256 hex digest를 반환한다."""
    return hashlib.sha256(value).hexdigest()


def _stable_id(prefix: str, value: dict[str, object]) -> str:
    """canonical payload로 content-addressed 식별자를 만든다."""
    return f"{prefix}-{_sha256(_canonical_json_bytes(value))}"


def _plan_identity_payload(
    *,
    hypothesis_id: str,
    control_id: str,
    candidate_ids: tuple[str, ...],
    created_at: datetime,
) -> dict[str, object]:
    """plan ID에 포함할 사전 선언 내용을 canonical mapping으로 만든다."""
    return {
        "hypothesis_id": hypothesis_id,
        "control_id": control_id,
        "candidate_ids": candidate_ids,
        "policy_version": PROMOTION_POLICY_VERSION,
        "created_at": _normalize_utc_datetime(created_at),
    }


class ExperimentPlan(_ImmutableModel):
    """학습 시작 전에 고정하는 가설·control·candidate·정책 선언."""

    plan_id: str = Field(min_length=1)
    hypothesis_id: str = Field(min_length=1)
    control_id: str = Field(min_length=1)
    candidate_ids: tuple[Annotated[str, Field(min_length=1)], ...] = Field(min_length=1)
    policy_version: Literal["promotion-policy-v1"] = PROMOTION_POLICY_VERSION
    created_at: datetime

    @field_validator("created_at")
    @classmethod
    def _normalize_created_at(cls, value: datetime) -> datetime:
        return _normalize_utc_datetime(value)

    @model_validator(mode="after")
    def _validate_content_addressed_plan_id(self) -> "ExperimentPlan":
        expected = _stable_id(
            "experiment-plan",
            _plan_identity_payload(
                hypothesis_id=self.hypothesis_id,
                control_id=self.control_id,
                candidate_ids=self.candidate_ids,
                created_at=self.created_at,
            ),
        )
        if self.plan_id != expected:
            raise ValueError("plan_id가 canonical experiment plan 내용과 다릅니다")
        return self


class GcsObjectReceipt(_ImmutableModel):
    """GCS server metadata와 byte hash로 object version을 고정하는 receipt."""

    uri: str = Field(min_length=1)
    generation: str = Field(min_length=1)
    metageneration: str = Field(min_length=1)
    time_created: datetime
    sha256: str = Field(pattern=SHA256_PATTERN)

    @field_validator("time_created")
    @classmethod
    def _normalize_time_created(cls, value: datetime) -> datetime:
        return _normalize_utc_datetime(value)


class ExperimentPlanReceipt(_ImmutableModel):
    """canonical plan과 그 plan을 담은 immutable GCS object receipt."""

    plan: ExperimentPlan
    object: GcsObjectReceipt


class HeldOutMetricEvidence(_ImmutableModel):
    """하나의 training run이 계산한 held-out ROC-AUC와 provenance binding."""

    run_id: str = Field(min_length=1)
    plan_receipt: ExperimentPlanReceipt
    metric_name: Literal["roc_auc"] = "roc_auc"
    dataset_split: Literal["test"] = "test"
    value: float = Field(ge=0, le=1)
    split_manifest_sha256: str = Field(pattern=SHA256_PATTERN)
    test_membership_sha256: str = Field(pattern=SHA256_PATTERN)
    model_artifact_path: str = Field(min_length=1)
    model_artifact_sha256: str = Field(pattern=SHA256_PATTERN)


class HeldOutMetricReceipt(_ImmutableModel):
    """held-out metric payload와 immutable GCS object receipt."""

    evidence: HeldOutMetricEvidence
    object: GcsObjectReceipt


@dataclass(frozen=True)
class _GcsRoot:
    """검증된 promotion evidence GCS root의 bucket·prefix 좌표."""

    bucket: str
    prefix: str


def _parse_gcs_root(uri: str) -> _GcsRoot:
    """명시적 evidence root를 canonical gs://bucket/prefix로 검증한다."""
    parsed = urlparse(uri)
    if (
        parsed.scheme != "gs"
        or not parsed.netloc
        or not parsed.path
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise PromotionEvidenceValidationError(
            "promotion evidence root는 gs://bucket/prefix 형식이어야 합니다"
        )
    raw_prefix = parsed.path.lstrip("/")
    if parsed.path.startswith("//") or raw_prefix.endswith("/"):
        raise PromotionEvidenceValidationError(
            "promotion evidence root는 중복 또는 끝 separator를 포함할 수 없습니다"
        )
    components = raw_prefix.split("/")
    if any(component in {"", ".", ".."} for component in components):
        raise PromotionEvidenceValidationError(
            "promotion evidence root는 빈, . 또는 .. component를 포함할 수 없습니다"
        )
    return _GcsRoot(bucket=parsed.netloc, prefix=raw_prefix)


def _parse_receipt_uri(uri: str, *, root: _GcsRoot) -> str:
    """receipt object URI가 configured root 바로 아래인지 검증하고 object name을 준다."""
    parsed = urlparse(uri)
    if (
        parsed.scheme != "gs"
        or parsed.netloc != root.bucket
        or parsed.params
        or parsed.query
        or parsed.fragment
    ):
        raise PromotionEvidenceValidationError("receipt URI가 promotion evidence root와 다릅니다")
    object_name = parsed.path.lstrip("/")
    if not object_name.startswith(f"{root.prefix}/"):
        raise PromotionEvidenceValidationError("receipt URI가 promotion evidence prefix 밖입니다")
    return object_name


def create_experiment_plan(
    *,
    hypothesis_id: str,
    control_id: str,
    candidate_ids: tuple[str, ...],
    created_at: datetime | None = None,
) -> ExperimentPlan:
    """사전 선언된 experiment plan을 content-addressed immutable 계약으로 만든다."""
    plan_time = _normalize_utc_datetime(created_at or datetime.now(timezone.utc))
    payload = _plan_identity_payload(
        hypothesis_id=hypothesis_id,
        control_id=control_id,
        candidate_ids=candidate_ids,
        created_at=plan_time,
    )
    return ExperimentPlan(
        plan_id=_stable_id("experiment-plan", payload),
        **payload,
    )


class PromotionEvidenceStore:
    """infra #485 evidence root에 대한 create-only write와 pinned verification adapter."""

    def __init__(self, evidence_root: str, *, client: object | None = None) -> None:
        self._root = _parse_gcs_root(evidence_root)
        if client is None:
            from google.cloud import storage

            client = storage.Client()
        self._client = client

    def _bucket(self) -> object:
        """configured bucket을 backend에서 얻고 안전한 오류로 감싼다."""
        try:
            return self._client.bucket(self._root.bucket)
        except Exception:
            raise PromotionEvidenceValidationError(
                "promotion evidence bucket을 초기화할 수 없습니다"
            ) from None

    def _object_receipt(self, blob: object, *, payload: bytes) -> GcsObjectReceipt:
        """reload된 blob metadata와 현재 payload byte로 immutable receipt를 만든다."""
        generation = getattr(blob, "generation", None)
        metageneration = getattr(blob, "metageneration", None)
        time_created = getattr(blob, "time_created", None)
        name = getattr(blob, "name", None)
        if generation is None or metageneration is None or time_created is None or not name:
            raise PromotionEvidenceValidationError("promotion evidence object metadata가 불완전합니다")
        return GcsObjectReceipt(
            uri=f"gs://{self._root.bucket}/{name}",
            generation=str(generation),
            metageneration=str(metageneration),
            time_created=time_created,
            sha256=_sha256(payload),
        )

    def _publish(self, *, object_name: str, payload: bytes) -> GcsObjectReceipt:
        """새 object만 게시하고 서버 metadata를 re-read한 receipt로 반환한다."""
        try:
            blob = self._bucket().blob(object_name)
            blob.upload_from_string(
                payload,
                content_type="application/json",
                if_generation_match=0,
            )
            blob.reload()
            return self._object_receipt(blob, payload=payload)
        except PromotionEvidenceValidationError:
            raise
        except Exception:
            raise PromotionEvidenceValidationError(
                "promotion evidence publish에 실패했습니다"
            ) from None

    def _read_receipted_bytes(self, receipt: GcsObjectReceipt) -> bytes:
        """receipt generation만 읽고 metadata·byte hash가 모두 맞는지 검증한다."""
        object_name = _parse_receipt_uri(receipt.uri, root=self._root)
        try:
            blob = self._bucket().blob(object_name, generation=int(receipt.generation))
            blob.reload()
            payload = blob.download_as_bytes()
        except Exception:
            raise PromotionEvidenceValidationError(
                "promotion evidence receipt generation을 읽을 수 없습니다"
            ) from None

        actual = self._object_receipt(blob, payload=payload)
        if actual.generation != receipt.generation:
            raise PromotionEvidenceValidationError("promotion evidence generation이 receipt와 다릅니다")
        if actual.metageneration != receipt.metageneration:
            raise PromotionEvidenceValidationError(
                "promotion evidence metageneration이 receipt와 다릅니다"
            )
        if actual.time_created != receipt.time_created:
            raise PromotionEvidenceValidationError(
                "promotion evidence time_created가 receipt와 다릅니다"
            )
        if actual.sha256 != receipt.sha256:
            raise PromotionEvidenceValidationError("promotion evidence sha256이 receipt와 다릅니다")
        return payload

    def publish_plan(self, plan: ExperimentPlan) -> ExperimentPlanReceipt:
        """plan ID path에 create-only로 plan body를 쓰고 receipt를 반환한다."""
        payload = _canonical_json_bytes(plan)
        receipt = self._publish(
            object_name=f"{self._root.prefix}/plans/{plan.plan_id}.json",
            payload=payload,
        )
        return ExperimentPlanReceipt(plan=plan, object=receipt)

    def verify_plan_receipt(self, receipt: ExperimentPlanReceipt) -> ExperimentPlan:
        """receipt가 가리키는 pinned object를 재검증하고 plan을 반환한다."""
        payload = self._read_receipted_bytes(receipt.object)
        try:
            plan = ExperimentPlan.model_validate_json(payload)
        except Exception:
            raise PromotionEvidenceValidationError(
                "promotion experiment plan body가 유효하지 않습니다"
            ) from None
        if plan != receipt.plan:
            raise PromotionEvidenceValidationError("promotion experiment plan body가 receipt와 다릅니다")
        expected_name = f"{self._root.prefix}/plans/{plan.plan_id}.json"
        if _parse_receipt_uri(receipt.object.uri, root=self._root) != expected_name:
            raise PromotionEvidenceValidationError("promotion experiment plan object path가 유효하지 않습니다")
        return plan

    def publish_held_out_metric(self, evidence: HeldOutMetricEvidence) -> HeldOutMetricReceipt:
        """held-out metric body를 training run의 create-only prefix에 게시한다."""
        payload = _canonical_json_bytes(evidence)
        evidence_sha256 = _sha256(payload)
        receipt = self._publish(
            object_name=(
                f"{self._root.prefix}/metrics/{evidence.run_id}/{evidence_sha256}.json"
            ),
            payload=payload,
        )
        return HeldOutMetricReceipt(evidence=evidence, object=receipt)

    def verify_held_out_metric_receipt(
        self, receipt: HeldOutMetricReceipt
    ) -> HeldOutMetricEvidence:
        """pinned metric body·receipt·content-addressed path의 결합을 재검증한다."""
        payload = self._read_receipted_bytes(receipt.object)
        try:
            evidence = HeldOutMetricEvidence.model_validate_json(payload)
        except Exception:
            raise PromotionEvidenceValidationError(
                "held-out metric body가 유효하지 않습니다"
            ) from None
        if evidence != receipt.evidence:
            raise PromotionEvidenceValidationError("held-out metric body가 receipt와 다릅니다")
        expected_name = (
            f"{self._root.prefix}/metrics/{evidence.run_id}/{_sha256(payload)}.json"
        )
        if _parse_receipt_uri(receipt.object.uri, root=self._root) != expected_name:
            raise PromotionEvidenceValidationError("held-out metric object path가 유효하지 않습니다")
        return evidence
