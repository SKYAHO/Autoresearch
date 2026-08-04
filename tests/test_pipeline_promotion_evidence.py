"""write-once 실험 승격 evidence GCS adapter 단위 테스트."""

from __future__ import annotations

import hashlib
import traceback
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone

import pytest
from pydantic import ValidationError

from src.pipeline.promotion_evidence import (
    _canonical_json_bytes,
    _sha256,
)
from src.pipeline.promotion_evidence import (
    SUPPORTED_HELD_OUT_METRIC_NAMES,
    HeldOutMetricEvidence,
    PromotionEvidenceStore,
    PromotionEvidenceValidationError,
    create_experiment_plan,
)

# #493 2단계 이전(`metric_name: Literal["roc_auc"]`, `value: Field(ge=0, le=1)`)
# 스키마로 실제 직렬화해 얻은 canonical JSON byte와 그 sha256이다. GCS에 이미
# 불변 객체로 쌓인 roc_auc receipt를 대표하며, 스키마 확장이 이 byte를 바꾸면
# 기존 `GcsObjectReceipt.sha256`이 전부 어긋난다.
_LEGACY_ROC_AUC_EVIDENCE_JSON = (
    b'{"dataset_split":"test","metric_name":"roc_auc","model_artifact_path":'
    b'"model/lgbm_model.joblib","model_artifact_sha256":"'
    + b"c" * 64
    + b'","plan_receipt":{"object":{"generation":"1","metageneration":"1",'
    b'"sha256":"' + b"d" * 64 + b'","time_created":"2026-08-01T00:00:00Z",'
    b'"uri":"gs://evidence/promotion-evidence/plans/experiment-plan-'
    b'ed4864a41e386d45c91a1d5eef974e20a5ed69435014506ba69ecfa6177ac20e.json"},'
    b'"plan":{"candidate_ids":["candidate-revision"],"control_id":'
    b'"control-revision","created_at":"2026-07-31T00:00:00Z","hypothesis_id":'
    b'"issue-466-h1","plan_id":"experiment-plan-'
    b'ed4864a41e386d45c91a1d5eef974e20a5ed69435014506ba69ecfa6177ac20e",'
    b'"policy_version":"promotion-policy-v1"}},"run_id":"run-42",'
    b'"split_manifest_sha256":"' + b"a" * 64 + b'","test_membership_sha256":"'
    + b"b" * 64
    + b'","value":0.8125}'
)
_LEGACY_ROC_AUC_EVIDENCE_SHA256 = (
    "1da6e627e125de56e794f4e0af58fc165e3b21169060b16c16efaaf929f4d8db"
)


@dataclass
class _StoredObject:
    """fake GCS object version의 server metadata와 byte를 보관한다."""

    payload: bytes
    generation: int
    metageneration: int
    time_created: datetime


class _FakeBlob:
    """필요한 google-cloud-storage Blob API만 흉내 낸다."""

    def __init__(self, bucket: "_FakeBucket", name: str, generation: int | None) -> None:
        self._bucket = bucket
        self.name = name
        self._requested_generation = generation
        self.generation: int | None = generation
        self.metageneration: int | None = None
        self.time_created: datetime | None = None

    def reload(self) -> None:
        stored = self._bucket.get(self.name, self._requested_generation)
        self.generation = stored.generation
        self.metageneration = stored.metageneration
        self.time_created = stored.time_created

    def upload_from_string(
        self,
        payload: bytes,
        *,
        content_type: str,
        if_generation_match: int,
    ) -> None:
        assert content_type == "application/json"
        self._bucket.create(self.name, payload, if_generation_match=if_generation_match)

    def download_as_bytes(self) -> bytes:
        return self._bucket.get(self.name, self._requested_generation).payload


class _FakeBucket:
    """generation별 object를 보존하는 create-only bucket fake."""

    def __init__(self, created_at: datetime) -> None:
        self._created_at = created_at
        self._objects: dict[tuple[str, int], _StoredObject] = {}
        self.blob_calls: list[tuple[str, int | None]] = []

    def blob(self, name: str, generation: int | None = None) -> _FakeBlob:
        self.blob_calls.append((name, generation))
        return _FakeBlob(self, name, generation)

    def create(self, name: str, payload: bytes, *, if_generation_match: int) -> None:
        if if_generation_match != 0:
            raise RuntimeError("create-only precondition required")
        if any(object_name == name for object_name, _ in self._objects):
            raise RuntimeError("precondition failed")
        generation = 1
        self._objects[(name, generation)] = _StoredObject(
            payload=payload,
            generation=generation,
            metageneration=1,
            time_created=self._created_at,
        )

    def get(self, name: str, generation: int | None) -> _StoredObject:
        if generation is None:
            generations = [key_generation for object_name, key_generation in self._objects if object_name == name]
            if not generations:
                raise RuntimeError("object not found")
            generation = max(generations)
        try:
            return self._objects[(name, generation)]
        except KeyError as error:
            raise RuntimeError("object generation not found") from error

    def replace_payload(self, name: str, generation: int, payload: bytes) -> None:
        stored = self.get(name, generation)
        self._objects[(name, generation)] = _StoredObject(
            payload=payload,
            generation=stored.generation,
            metageneration=stored.metageneration,
            time_created=stored.time_created,
        )

    def add_new_generation(self, name: str, payload: bytes) -> None:
        generations = [key_generation for object_name, key_generation in self._objects if object_name == name]
        generation = max(generations) + 1
        self._objects[(name, generation)] = _StoredObject(
            payload=payload,
            generation=generation,
            metageneration=1,
            time_created=self._created_at + timedelta(seconds=generation),
        )


class _FakeStorageClient:
    """단일 bucket만 제공하는 Storage client fake."""

    def __init__(self, bucket: _FakeBucket) -> None:
        self._bucket = bucket

    def bucket(self, name: str) -> _FakeBucket:
        assert name == "evidence"
        return self._bucket


class _FailingStorageClient:
    """backend 원문이 안전 오류로 새지 않는지 검증하는 fake."""

    def bucket(self, name: str) -> _FakeBucket:
        raise RuntimeError("credential=synthetic-private-value")


def _store() -> tuple[PromotionEvidenceStore, _FakeBucket]:
    bucket = _FakeBucket(datetime(2026, 8, 1, tzinfo=timezone.utc))
    return (
        PromotionEvidenceStore(
            "gs://evidence/promotion-evidence",
            client=_FakeStorageClient(bucket),
        ),
        bucket,
    )


def _plan():
    return create_experiment_plan(
        hypothesis_id="issue-466-h1",
        control_id="control-revision",
        candidate_ids=("candidate-revision",),
        created_at=datetime(2026, 7, 31, tzinfo=timezone.utc),
    )


def test_publish_plan_creates_content_addressed_object_and_revalidates_receipt() -> None:
    store, bucket = _store()
    plan = _plan()

    receipt = store.publish_plan(plan)

    object_name = f"promotion-evidence/plans/{plan.plan_id}.json"
    assert bucket.blob_calls == [(object_name, None)]
    assert receipt.plan == plan
    assert receipt.object.uri == f"gs://evidence/{object_name}"
    assert receipt.object.generation == "1"
    assert receipt.object.metageneration == "1"
    assert receipt.object.time_created == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert receipt.object.sha256 == hashlib.sha256(
        bucket.get(object_name, 1).payload
    ).hexdigest()

    assert store.verify_plan_receipt(receipt) == plan
    assert bucket.blob_calls[-1] == (object_name, 1)


def test_verify_plan_receipt_rejects_tampered_pinned_generation_without_latest_fallback() -> None:
    store, bucket = _store()
    receipt = store.publish_plan(_plan())
    object_name = "promotion-evidence/plans/" + receipt.plan.plan_id + ".json"
    bucket.add_new_generation(object_name, b'{"not":"the pinned plan"}')
    bucket.replace_payload(object_name, 1, b'{"tampered":true}')

    with pytest.raises(PromotionEvidenceValidationError, match="sha256"):
        store.verify_plan_receipt(receipt)

    assert bucket.blob_calls[-1] == (object_name, 1)


def test_publish_plan_treats_duplicate_create_precondition_as_failure() -> None:
    store, _ = _store()
    plan = _plan()
    store.publish_plan(plan)

    with pytest.raises(PromotionEvidenceValidationError, match="publish"):
        store.publish_plan(plan)


def test_backend_error_does_not_expose_its_raw_message_in_traceback() -> None:
    store = PromotionEvidenceStore(
        "gs://evidence/promotion-evidence",
        client=_FailingStorageClient(),
    )

    with pytest.raises(PromotionEvidenceValidationError) as raised:
        store.publish_plan(_plan())

    formatted = "".join(traceback.format_exception(raised.value))
    assert "synthetic-private-value" not in formatted


@pytest.mark.parametrize(
    ("field_name", "value", "error_fragment"),
    [
        ("generation", "999", "receipt generation"),
        ("metageneration", "999", "metageneration"),
        ("sha256", "f" * 64, "sha256"),
        ("uri", "gs://evidence/outside/plan.json", "prefix 밖"),
    ],
)
def test_verify_plan_receipt_fails_closed_for_tampered_receipt_fields(
    field_name: str, value: str, error_fragment: str
) -> None:
    store, _ = _store()
    receipt = store.publish_plan(_plan())
    tampered = receipt.model_copy(
        update={"object": receipt.object.model_copy(update={field_name: value})}
    )

    with pytest.raises(PromotionEvidenceValidationError, match=error_fragment):
        store.verify_plan_receipt(tampered)


def _metric(plan_receipt, **overrides: object) -> HeldOutMetricEvidence:
    fields: dict[str, object] = {
        "run_id": "run-42",
        "plan_receipt": plan_receipt,
        "value": 0.8125,
        "split_manifest_sha256": "a" * 64,
        "test_membership_sha256": "b" * 64,
        "model_artifact_path": "model/lgbm_model.joblib",
        "model_artifact_sha256": "c" * 64,
    }
    fields.update(overrides)
    return HeldOutMetricEvidence(**fields)


def test_publish_metric_binds_plan_run_split_and_model_hash() -> None:
    store, bucket = _store()
    plan_receipt = store.publish_plan(_plan())
    metric = _metric(plan_receipt)

    receipt = store.publish_held_out_metric(metric)

    assert receipt.evidence == metric
    assert receipt.object.uri.startswith("gs://evidence/promotion-evidence/metrics/run-42/")
    assert bucket.blob_calls[-1][0].startswith("promotion-evidence/metrics/run-42/")
    assert store.verify_held_out_metric_receipt(receipt) == metric


def test_existing_roc_auc_evidence_deserializes_with_unchanged_sha256() -> None:
    """확장된 스키마가 기존 roc_auc receipt의 byte와 sha256을 바꾸지 않는다(#493 D3).

    이미 GCS에 쌓인 evidence는 마이그레이션하지 않으므로, ① 옛 byte가 그대로
    역직렬화되고 ② 그 값을 다시 canonical 직렬화하면 byte가 동일하며 ③ sha256이
    변하지 않아야 한다. 셋 중 하나라도 깨지면 기존 `GcsObjectReceipt.sha256`과
    content-addressed object key가 전부 어긋난다.
    """
    evidence = HeldOutMetricEvidence.model_validate_json(_LEGACY_ROC_AUC_EVIDENCE_JSON)

    assert evidence.metric_name == "roc_auc"
    assert evidence.value == 0.8125
    assert _canonical_json_bytes(evidence) == _LEGACY_ROC_AUC_EVIDENCE_JSON
    assert (
        hashlib.sha256(_LEGACY_ROC_AUC_EVIDENCE_JSON).hexdigest()
        == _LEGACY_ROC_AUC_EVIDENCE_SHA256
    )
    assert _sha256(_canonical_json_bytes(evidence)) == _LEGACY_ROC_AUC_EVIDENCE_SHA256


def test_existing_roc_auc_evidence_keeps_its_content_addressed_object_key() -> None:
    """기존 roc_auc receipt의 object key가 스키마 확장 뒤에도 동일하다."""
    store, bucket = _store()
    plan_receipt = store.publish_plan(_plan())
    plan_receipt = plan_receipt.model_copy(
        update={"object": plan_receipt.object.model_copy(update={"sha256": "d" * 64})}
    )

    receipt = store.publish_held_out_metric(_metric(plan_receipt))

    assert bucket.blob_calls[-1][0] == (
        f"promotion-evidence/metrics/run-42/{_LEGACY_ROC_AUC_EVIDENCE_SHA256}.json"
    )
    assert receipt.object.sha256 == _LEGACY_ROC_AUC_EVIDENCE_SHA256


def test_one_run_publishes_every_policy_metric_to_a_distinct_object_key() -> None:
    """한 run이 지표 3건을 써도 create-only key가 충돌하지 않는다(#493 D3)."""
    store, bucket = _store()
    plan_receipt = store.publish_plan(_plan())
    values = {"roc_auc": 0.8125, "pr_auc": 0.4, "log_loss": 0.37}

    receipts = [
        store.publish_held_out_metric(
            _metric(plan_receipt, metric_name=name, value=values[name])
        )
        for name in SUPPORTED_HELD_OUT_METRIC_NAMES
    ]

    keys = {receipt.object.uri for receipt in receipts}
    assert len(keys) == len(SUPPORTED_HELD_OUT_METRIC_NAMES)
    for name, receipt in zip(SUPPORTED_HELD_OUT_METRIC_NAMES, receipts):
        assert receipt.object.uri.startswith(
            "gs://evidence/promotion-evidence/metrics/run-42/"
        )
        assert store.verify_held_out_metric_receipt(receipt).metric_name == name
    assert len({name for name, _ in bucket.blob_calls}) == 1 + len(receipts)


def test_metric_name_outside_the_policy_allowlist_is_rejected() -> None:
    store, _ = _store()
    plan_receipt = store.publish_plan(_plan())

    with pytest.raises(ValidationError, match="allowlist"):
        _metric(plan_receipt, metric_name="brier")


@pytest.mark.parametrize(
    ("metric_name", "value"),
    [
        ("roc_auc", 0.0),
        ("roc_auc", 1.0),
        ("pr_auc", 0.5),
        ("log_loss", 0.0),
        ("log_loss", 12.5),
    ],
)
def test_metric_value_bounds_accept_in_range_values(metric_name: str, value: float) -> None:
    store, _ = _store()
    plan_receipt = store.publish_plan(_plan())

    evidence = _metric(plan_receipt, metric_name=metric_name, value=value)

    assert evidence.value == value


@pytest.mark.parametrize(
    ("metric_name", "value"),
    [
        ("roc_auc", 1.5),
        ("roc_auc", -0.1),
        ("pr_auc", 1.0000001),
        ("log_loss", -0.000001),
        ("log_loss", float("nan")),
        ("log_loss", float("inf")),
        ("roc_auc", float("nan")),
    ],
)
def test_metric_value_outside_its_metric_range_is_rejected(
    metric_name: str, value: float
) -> None:
    store, _ = _store()
    plan_receipt = store.publish_plan(_plan())

    with pytest.raises(ValidationError):
        _metric(plan_receipt, metric_name=metric_name, value=value)
