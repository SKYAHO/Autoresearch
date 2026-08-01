# Write-once Promotion Evidence Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 학습 전에 GCS에 한 번만 기록한 실험 계획과 active training run이 기록한 held-out ROC-AUC를 재검증하여, 30개 paired seed의 eligible/hold/reject 판정이 호출자 JSON이나 사후 plan에 의존하지 않게 한다.

**Architecture:** src/pipeline/promotion_evidence.py가 GCS generation-pinned object와 receipt의 유일한 application adapter가 된다. Training은 검증된 plan receipt를 split manifest에 바인딩하고 model artifact hash와 held-out metric receipt를 남긴다. Comparison과 evaluator는 receipt의 GCS object를 다시 읽어 hash·generation·server time·run/split/model binding을 검증한 값만 사용한다.

**Tech Stack:** Python 3.11, Pydantic v2, google-cloud-storage (기존 의존성), MLflow, LightGBM, pytest, Typer, Ruff.

## Global Constraints

- 승격 정책은 promotion-policy-v1: test split roc_auc 최대화, 정확히 paired seed 42..71, 양측 95% t 신뢰구간 하한 양수일 때만 eligible이다.
- plan.created_at은 감사 필드일 뿐 신뢰 근거가 아니다. GCS가 발급한 time_created, generation, metageneration, byte SHA-256만 immutable receipt의 시간·내용 근거다.
- plan과 metric write는 if_generation_match=0으로만 수행한다. 덮어쓰기·삭제·production prefix 권한 차단은 infra #485가 집행하며 application은 그 precondition 실패를 성공으로 취급하지 않는다.
- GCS root는 필수 환경 변수를 추가하지 않고 명시적 --promotion-evidence-root gs://bucket/prefix 인자로만 받는다. 이 CLI들은 현재 Airflow public batch contract 대상이 아니므로 docs/specs/2026-07-13-public-batch-execution-contract.md를 변경하지 않는다.
- receipt가 없는 기존 manifest는 parse 가능해야 한다. 다만 automatic evaluation은 반드시 hold하며, plan/metric receipt가 일부만 있거나 서로 맞지 않으면 comparison output을 만들지 않는다.
- MLflow artifact는 snapshot/split/model의 재현성 검증에 사용하되, completed run에도 artifact가 추가될 수 있으므로 plan 사전 선언의 시간 근거로 사용하지 않는다.
- application은 registry alias를 이동하지 않는다. dev/production alias, compare-and-swap, lock, rollback은 #470 후속 범위다.
- 새 모듈에는 CLAUDE.md의 pipeline responsibility docstring을 작성한다. 새 의존성·시크릿·필수 환경 변수·Airflow DAG 변경을 추가하지 않는다.

## File Structure

| 경로 | 책임 |
| --- | --- |
| src/pipeline/promotion_evidence.py | immutable plan/metric/receipt Pydantic 계약, canonical SHA-256, GCS write-once publish, generation-pinned re-read와 안전한 검증 오류 |
| src/pipeline/training_provenance.py | split/comparison manifest에 선택적 promotion evidence binding을 저장하는 계약 |
| src/pipeline/train.py | training 전 plan receipt 검증, split binding, active run의 held-out ROC-AUC와 model hash를 metric object로 기록 |
| src/pipeline/training_comparison.py | 두 run의 plan/metric receipt 및 MLflow server time을 재검증하여 verified comparison에 evidence를 연결 |
| src/pipeline/experiment_evaluation.py | raw 숫자 대신 verified receipt를 다시 읽어 30 seed 통계와 deterministic verdict를 계산 |
| src/pipeline/evaluate.py | training과 standalone evaluation이 같은 held-out ROC-AUC feature preparation을 공유하는 순수 helper |
| src/cli.py | plan publish, training/pipeline receipt 전달, evidence-aware comparison CLI adapter |
| tests/test_pipeline_promotion_evidence.py | fake GCS client로 precondition, pinned read, metadata/hash/path 검증을 단위 테스트 |
| tests/test_pipeline_train.py | active-run metric publish와 plan receipt binding, partial option fail-closed를 테스트 |
| tests/test_training_comparison.py | valid evidence, late plan, metric/model/run binding 오류에서 output 미게시를 테스트 |
| tests/test_pipeline_experiment_evaluation.py | receipt re-read만 통계 입력으로 쓰고 legacy/invalid evidence가 hold되는 것을 테스트 |
| tests/test_cli.py | 새 선택 인자·plan publish와 안전한 오류 adapter를 테스트 |
| docs/guides/training-experiment-provenance.md | 실제 구현된 CLI·artifact 경로·검증/legacy 동작으로 설계를 갱신 |

---

### Task 1: Write-once GCS evidence contract and adapter

**Files:**

- Create: src/pipeline/promotion_evidence.py
- Create: tests/test_pipeline_promotion_evidence.py
- Modify: src/pipeline/experiment_evaluation.py

**Interfaces:**

- Produces: ExperimentPlan, ExperimentPlanReceipt, HeldOutMetricEvidence, HeldOutMetricReceipt, GcsObjectReceipt, PromotionEvidenceStore, PromotionEvidenceValidationError, create_experiment_plan().
- PromotionEvidenceStore(evidence_root: str, client: object | None = None) exposes publish_plan(plan) -> ExperimentPlanReceipt, verify_plan_receipt(receipt) -> ExperimentPlan, publish_held_out_metric(evidence) -> HeldOutMetricReceipt, and verify_held_out_metric_receipt(receipt) -> HeldOutMetricEvidence.
- experiment_evaluation.py re-exports ExperimentPlan and create_experiment_plan from this module during the compatibility transition; it must not own a second copy of the model.

- [x] **Step 1: Write the failing object receipt tests**

~~~python
def test_publish_plan_uses_create_only_path_and_returns_server_receipt() -> None:
    store, bucket = _store_with_fake_gcs(
        root="gs://evidence/promotion-evidence",
        time_created=datetime(2026, 8, 1, tzinfo=timezone.utc),
    )
    plan = create_experiment_plan(
        hypothesis_id="issue-466-h1",
        control_id="control-revision",
        candidate_ids=("candidate-revision",),
        created_at=datetime(2026, 7, 1, tzinfo=timezone.utc),
    )

    receipt = store.publish_plan(plan)

    assert bucket.uploads == [(f"promotion-evidence/plans/{plan.plan_id}.json", 0)]
    assert receipt.object.uri == f"gs://evidence/promotion-evidence/plans/{plan.plan_id}.json"
    assert receipt.object.generation == "1"
    assert receipt.object.time_created == datetime(2026, 8, 1, tzinfo=timezone.utc)
    assert store.verify_plan_receipt(receipt) == plan
~~~

Add negative tests that mutate returned byte content, receipt generation, receipt SHA-256, object metageneration, or root/path. Assert PromotionEvidenceValidationError and never fall back to an unpinned latest object. Add a fake `upload_from_string(payload, content_type="application/json", if_generation_match=0)` that raises on a second write and assert the adapter raises rather than returning a receipt.

- [x] **Step 2: Run the new tests to verify they fail**

Run: uv run python -m pytest tests/test_pipeline_promotion_evidence.py -v

Expected: FAIL during collection because src.pipeline.promotion_evidence does not exist.

- [x] **Step 3: Implement immutable evidence models and canonical serialization**

~~~python
class GcsObjectReceipt(_ImmutableModel):
    uri: str
    generation: str = Field(min_length=1)
    metageneration: str = Field(min_length=1)
    time_created: datetime
    sha256: str = Field(pattern=SHA256_PATTERN)


class ExperimentPlanReceipt(_ImmutableModel):
    plan: ExperimentPlan
    object: GcsObjectReceipt


class HeldOutMetricEvidence(_ImmutableModel):
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
    evidence: HeldOutMetricEvidence
    object: GcsObjectReceipt
~~~

Use sorted-key UTF-8 JSON with compact separators for every SHA-derived ID and stored body. Normalize all receipt timestamps to timezone-aware UTC and reject gs:// roots with no bucket, empty prefix, duplicate slash, . or .. component. Keep plan IDs content-addressed using hypothesis, control, candidates, policy version, and audit timestamp exactly as the current implementation does.

- [x] **Step 4: Implement the GCS adapter with pinned reads**

~~~python
def _read_receipted_bytes(self, receipt: GcsObjectReceipt) -> bytes:
    bucket_name, object_name = _parse_gs_uri(receipt.uri, expected_root=self._root)
    blob = self._client.bucket(bucket_name).blob(object_name, generation=int(receipt.generation))
    blob.reload()
    payload = blob.download_as_bytes()
    _assert_object_metadata(blob, receipt)
    _assert_sha256(payload, receipt.sha256)
    return payload


def publish_plan(self, plan: ExperimentPlan) -> ExperimentPlanReceipt:
    payload = _canonical_json_bytes(plan)
    blob = self._bucket.blob(f"{self._prefix}/plans/{plan.plan_id}.json")
    blob.upload_from_string(payload, content_type="application/json", if_generation_match=0)
    blob.reload()
    return ExperimentPlanReceipt(plan=plan, object=_receipt_from_blob(blob, payload))
~~~

Derive the metric object name from its canonical evidence SHA: <prefix>/metrics/<run_id>/<metric-sha256>.json. Verify the parsed body against the receipt's nested model after rehashing; do not trust just metadata or just a caller-supplied Pydantic object. Wrap GCS/backend exceptions in the fixed safe validation error without copying credentials, signed URLs, or raw backend text.

- [x] **Step 5: Move shared plan symbols and run the adapter tests**

Replace local ExperimentPlan, immutable base model, canonical plan ID helpers, and create_experiment_plan() in experiment_evaluation.py with imports/re-exports from promotion_evidence.py. Preserve the existing public import names while the evaluator is refactored in Task 4.

Run: uv run python -m pytest tests/test_pipeline_promotion_evidence.py tests/test_pipeline_experiment_evaluation.py -v

Expected: PASS. Existing evaluation tests may still construct legacy raw evidence at this checkpoint; only the shared plan model moves here.

- [x] **Step 6: Commit the evidence adapter**

~~~bash
git add src/pipeline/promotion_evidence.py tests/test_pipeline_promotion_evidence.py src/pipeline/experiment_evaluation.py
git commit -m "feat: add write-once promotion evidence receipts"
~~~

### Task 2: Bind verified plans and held-out metrics to training

**Files:**

- Modify: src/pipeline/training_provenance.py
- Modify: src/pipeline/train.py
- Modify: src/pipeline/evaluate.py
- Modify: tests/test_pipeline_train.py

**Interfaces:**

- TrainingSplitManifest.experiment_plan_receipt: ExperimentPlanReceipt | None = None; old v1 JSON remains valid when absent.
- Extend `build_split_manifest` with `experiment_plan_receipt: ExperimentPlanReceipt | None = None`; its existing required `run_id`, snapshot, seed, split-position, and feature-column parameters are unchanged.
- Extend `train.main` with `experiment_plan_receipt_path: str | None = None`, `promotion_evidence_root: str | None = None`, and test-only `promotion_evidence_store: PromotionEvidenceStore | None = None`; the first two arguments must be supplied together.
- TrainingOutcome.held_out_metric_receipt: HeldOutMetricReceipt | None = None lets the caller observe a successfully published metric without using it as the trust source.
- evaluate_held_out_roc_auc(model: object, dataset: pd.DataFrame, feature_columns: Sequence[str]) -> float is shared by train and standalone evaluation.

- [x] **Step 1: Write failing training evidence tests**

~~~python
def test_main_binds_verified_plan_and_publishes_held_out_metric_inside_run(tmp_path, monkeypatch) -> None:
    config_path, data_path, tracking_uri = _prepared_verified_dataset(tmp_path, monkeypatch)
    store, _ = _store_with_fake_gcs(root="gs://evidence/promotion-evidence")
    receipt_path = _write_plan_receipt(tmp_path, store)

    outcome = train.main(
        config_path=str(config_path), data_path=str(data_path),
        model_output=str(tmp_path / "model.joblib"),
        test_set_output=str(tmp_path / "test.csv"),
        feature_columns_output=str(tmp_path / "features.json"),
        categorical_columns_output=str(tmp_path / "categories.json"),
        require_snapshot=True, defer_registration=True,
        experiment_plan_receipt_path=str(receipt_path),
        promotion_evidence_root="gs://evidence/promotion-evidence",
        promotion_evidence_store=store,
    )

    split = TrainingSplitManifest.model_validate_json(
        split_manifest_path(tmp_path / "test.csv").read_text(encoding="utf-8")
    )
    assert split.experiment_plan_receipt == _load_receipt(receipt_path)
    assert outcome.held_out_metric_receipt is not None
    metric = store.verify_held_out_metric_receipt(outcome.held_out_metric_receipt)
    assert metric.run_id == outcome.run_id
    assert metric.dataset_split == "test"
    assert metric.model_artifact_sha256 == sha256_file(tmp_path / "model.joblib")
~~~

Add tests that pass only one promotion option, request promotion without require_snapshot, supply a tampered local plan receipt, or make metric publish fail. Assert the first three fail before LGBMModel.fit; metric publish failure leaves no successful TrainingOutcome or automatic-evaluation artifact.

- [x] **Step 2: Run the targeted tests to verify they fail**

Run: uv run python -m pytest tests/test_pipeline_train.py::test_main_binds_verified_plan_and_publishes_held_out_metric_inside_run -v

Expected: FAIL because train.main has no promotion evidence arguments.

- [x] **Step 3: Extend provenance without changing legacy parsing**

~~~python
class TrainingSplitManifest(_ImmutableModel):
    # existing v1 fields remain unchanged
    experiment_plan_receipt: ExperimentPlanReceipt | None = None


def build_split_manifest(
    *,
    run_id: str,
    snapshot: TrainingSnapshotManifest,
    snapshot_manifest_sha256: str,
    seeds: TrainingSeeds,
    test_size: float,
    val_size: float,
    split_positions: Mapping[str, Sequence[int]],
    feature_columns: Sequence[str],
    experiment_plan_receipt: ExperimentPlanReceipt | None = None,
) -> TrainingSplitManifest:
    return TrainingSplitManifest(
        # existing fields
        experiment_plan_receipt=experiment_plan_receipt,
    )
~~~

Import only the receipt model; do not make training_provenance.py create network clients. Add a regression assertion that a pre-change TrainingSplitManifest JSON with no new key parses with experiment_plan_receipt is None.

- [x] **Step 4: Add one shared held-out ROC-AUC helper and use it from training**

~~~python
def evaluate_held_out_roc_auc(model: object, dataset: pd.DataFrame, feature_columns: Sequence[str]) -> float:
    features = dataset[list(feature_columns)].copy()
    for column in CATEGORICAL_FEATURE_COLUMNS:
        features[column] = features[column].astype("category")
    return float(roc_auc_score(dataset["clicked"], model.predict_proba(features)[:, 1]))
~~~

Have evaluate.main call this helper for ROC-AUC while retaining its PR-AUC, LogLoss, Brier, and calibration behavior. After model.save() and the existing model artifact upload, call the helper on the held-out test_df, build HeldOutMetricEvidence with sha256_file(model_path), the split manifest SHA, and split_manifest.splits["test"].membership_sha256, then publish it through the injected/default store while the mlflow.start_run context is active.

- [x] **Step 5: Verify required ordering and artifact correlation**

Keep promotion evidence disabled unless both options are present. When enabled, load the local receipt and run store.verify_plan_receipt() before model fit; pass that exact receipt to build_split_manifest. Write the metric receipt atomically, then upload it as `reproducibility/metrics/held_out_metric_receipt.json` for coordinate discovery only. Comparison/evaluation must later re-read GCS rather than trust this MLflow copy. Derive model_artifact_path as `model/<Path(model_path).name>` so comparison can download and hash the same MLflow file.

Run: uv run python -m pytest tests/test_pipeline_train.py -v

Expected: PASS, including existing legacy training tests that do not supply promotion options.

- [x] **Step 6: Commit the training binding**

~~~bash
git add src/pipeline/training_provenance.py src/pipeline/train.py src/pipeline/evaluate.py tests/test_pipeline_train.py
git commit -m "feat: bind held-out metrics to experiment plans"
~~~

### Task 3: Verify promotion evidence during fair comparison

**Files:**

- Modify: src/pipeline/promotion_evidence.py
- Modify: src/pipeline/training_provenance.py
- Modify: src/pipeline/training_comparison.py
- Modify: tests/test_training_comparison.py

**Interfaces:**

- VerifiedComparisonPromotionEvidence contains one ExperimentPlanReceipt, one baseline HeldOutMetricReceipt, and one challenger HeldOutMetricReceipt.
- TrainingComparisonManifest.promotion_evidence: VerifiedComparisonPromotionEvidence | None = None; experiment_plan_id is removed from new comparison creation and retained only as a parseable legacy field if required by historical artifacts.
- Extend `verify_training_comparison` with `promotion_evidence_store: PromotionEvidenceStore | None = None`; its existing run IDs and output path are unchanged. It creates legacy output only if both run split manifests have no plan receipt; any partial/malformed evidence raises ComparisonValidationError before output or challenger artifact publish.
- Extend the private `_VerifiedRun` with `held_out_metric_receipt: HeldOutMetricReceipt | None`, loaded only from `reproducibility/metrics/held_out_metric_receipt.json` and parsed before the comparison can use it.

- [x] **Step 1: Write failing comparison tests with a fake GCS store**

~~~python
def test_verify_comparison_rechecks_receipts_and_records_verified_metrics(tmp_path, monkeypatch) -> None:
    store, _ = _store_with_fake_gcs(root="gs://evidence/promotion-evidence")
    baseline_run, challenger_run = _log_two_runs_with_plan_and_metrics(tmp_path, store)

    result = verify_training_comparison(
        baseline_run, challenger_run, tmp_path / "comparison.json",
        promotion_evidence_store=store,
    )

    assert result.promotion_evidence is not None
    assert result.promotion_evidence.baseline_metric.evidence.run_id == baseline_run
    assert result.promotion_evidence.challenger_metric.evidence.run_id == challenger_run
~~~

Add parameterized tests for: GCS bytes changed after receipt creation, receipt generation changed, baseline/challenger plan receipt mismatch, plan time_created after either MLflow run start, metric time outside its run start/end, metric run ID/split hash/test membership/model artifact hash mismatch, and exactly one run carrying a plan receipt. Every case must assert no local comparison file and no reproducibility/comparisons upload.

- [x] **Step 2: Run the targeted comparison test to verify it fails**

Run: uv run python -m pytest tests/test_training_comparison.py::test_verify_comparison_rechecks_receipts_and_records_verified_metrics -v

Expected: FAIL because comparison has no promotion_evidence_store parameter or evidence output field.

- [x] **Step 3: Add the comparison evidence manifest model**

~~~python
class VerifiedComparisonPromotionEvidence(_ImmutableModel):
    plan_receipt: ExperimentPlanReceipt
    baseline_metric: HeldOutMetricReceipt
    challenger_metric: HeldOutMetricReceipt


class TrainingComparisonManifest(_ImmutableModel):
    # existing fair-comparison fields
    promotion_evidence: VerifiedComparisonPromotionEvidence | None = None
~~~

Use None as the only legacy representation. Do not make an incomplete object parse as legacy. Calculate the comparison ID from the two run IDs, common snapshot, challenger split hash, and the verified plan object SHA-256 when promotion evidence is present.

- [x] **Step 4: Re-read all GCS evidence and correlate it to MLflow artifacts**

~~~python
def _verify_promotion_evidence(
    *,
    baseline: _VerifiedRun,
    challenger: _VerifiedRun,
    baseline_run: mlflow.entities.Run,
    challenger_run: mlflow.entities.Run,
    store: PromotionEvidenceStore,
) -> VerifiedComparisonPromotionEvidence:
    plan_receipt = baseline.split.experiment_plan_receipt
    if plan_receipt is None or baseline.held_out_metric_receipt is None:
        raise ComparisonValidationError("baseline promotion evidence missing")
    if challenger.split.experiment_plan_receipt is None or challenger.held_out_metric_receipt is None:
        raise ComparisonValidationError("challenger promotion evidence missing")
    plan = store.verify_plan_receipt(plan_receipt)
    _assert_equal("plan receipt", plan_receipt, challenger.split.experiment_plan_receipt)
    _assert_plan_precedes_run(plan_receipt, baseline_run)
    _assert_plan_precedes_run(plan_receipt, challenger_run)
    baseline_metric = store.verify_held_out_metric_receipt(baseline.held_out_metric_receipt)
    challenger_metric = store.verify_held_out_metric_receipt(challenger.held_out_metric_receipt)
    _assert_metric_binding(baseline_metric, baseline)
    _assert_metric_binding(challenger_metric, challenger)
    return VerifiedComparisonPromotionEvidence(
        plan_receipt=plan_receipt,
        baseline_metric=baseline.held_out_metric_receipt,
        challenger_metric=challenger.held_out_metric_receipt,
    )
~~~

Read MLflow run start_time and end_time as UTC server timestamps. Require plan time_created <= start_time for both runs and start_time <= metric.time_created <= end_time for its own completed run. Download metric.model_artifact_path through the existing safe artifact helper, recalculate SHA-256, and compare to the metric body. Keep the existing snapshot/split/seed equality checks unchanged and invoke this new gate only after they pass.

- [x] **Step 5: Run all comparison tests and commit**

Run: uv run python -m pytest tests/test_training_comparison.py -v

Expected: PASS. Legacy fixtures with no receipt remain fair-comparison compatible; their resulting manifest has promotion_evidence is None.

~~~bash
git add src/pipeline/promotion_evidence.py src/pipeline/training_provenance.py src/pipeline/training_comparison.py tests/test_training_comparison.py
git commit -m "feat: verify write-once promotion evidence in comparisons"
~~~

### Task 4: Make the 30-seed evaluator consume verified receipts only

**Files:**

- Modify: src/pipeline/experiment_evaluation.py
- Modify: tests/test_pipeline_experiment_evaluation.py

**Interfaces:**

- PairedSeedObservation has seed: int and comparison: TrainingComparisonManifest; it no longer accepts caller-provided baseline/challenger metric values.
- PairedSeedEvidence has plan_receipt: ExperimentPlanReceipt and ordered observations.
- create_paired_seed_evidence(plan_receipt, observations) -> PairedSeedEvidence creates the stable evidence ID from receipt coordinates and comparison IDs.
- evaluate_experiment(evidence, *, promotion_evidence_store: PromotionEvidenceStore, evaluated_at: datetime | None = None) -> ExperimentEvaluation re-reads plan and metric receipts before calculating statistics.

- [x] **Step 1: Rewrite a passing raw-number test as a failing receipt test**

~~~python
def test_v1_marks_verified_positive_paired_30_seed_evidence_eligible() -> None:
    store, receipt = _published_plan_store()
    evidence = create_paired_seed_evidence(
        plan_receipt=receipt,
        observations=tuple(
            PairedSeedObservation(seed=seed, comparison=_verified_comparison(seed, store, receipt))
            for seed in POLICY_SEEDS
        ),
    )

    evaluation = evaluate_experiment(evidence, promotion_evidence_store=store, evaluated_at=PLAN_TIME)

    assert evaluation.verdict is EvaluationVerdict.ELIGIBLE
~~~

Replace tests that inject HeldOutRocAucEvidence with a caller-supplied metric value with receipts published to the fake store. Add tests that use valid-looking raw comparison fields but omit promotion_evidence, have a stale receipt byte SHA, or attach metrics from another split; each must return hold without estimating a confidence interval.

- [x] **Step 2: Run the rewritten tests to verify they fail**

Run: uv run python -m pytest tests/test_pipeline_experiment_evaluation.py -v

Expected: FAIL because observations still accept raw metric evidence and the evaluator has no store parameter.

- [x] **Step 3: Remove raw metric injection from the evaluation path**

~~~python
class PairedSeedObservation(_ImmutableModel):
    seed: int
    comparison: TrainingComparisonManifest


def _verified_metric_values(observation, store, plan_receipt) -> tuple[float, float]:
    promotion = observation.comparison.promotion_evidence
    if promotion is None:
        raise PromotionEvidenceValidationError("comparison promotion evidence missing")
    _assert_equal_receipt(promotion.plan_receipt, plan_receipt)
    baseline = store.verify_held_out_metric_receipt(promotion.baseline_metric)
    challenger = store.verify_held_out_metric_receipt(promotion.challenger_metric)
    return baseline.value, challenger.value
~~~

Verify the plan receipt first, then re-read each comparison's plan and metric receipts. Retain the current policy checks for single candidate, exact ordered seeds, equal snapshot, exact effective seed triplet, duplicate comparisons, and finite metric values. Replace obsolete reason codes with explicit PLAN_RECEIPT_MISSING, RECEIPT_REVALIDATION_FAILED, and METRIC_BINDING_MISMATCH codes; all such failures use the existing no-statistics hold result.

- [x] **Step 4: Verify eligible, reject, hold, and legacy behavior**

Run: uv run python -m pytest tests/test_pipeline_experiment_evaluation.py tests/test_pipeline_seed_sweep.py -v

Expected: PASS. Preserve the existing positive/negative/inconclusive t-interval assertions, including zero standard error recording its real df=29 critical value. Verify a legacy comparison with no promotion evidence produces hold, never eligible.

- [x] **Step 5: Commit the receipt-only evaluator**

~~~bash
git add src/pipeline/experiment_evaluation.py tests/test_pipeline_experiment_evaluation.py
git commit -m "feat: evaluate promotions from verified evidence receipts"
~~~

### Task 5: Add CLI adapters, finalize guide, and run the full verification gate

**Files:**

- Modify: src/cli.py
- Modify: tests/test_cli.py
- Modify: docs/guides/training-experiment-provenance.md

**Interfaces:**

- create-experiment-plan --hypothesis-id --control-id --candidate-id --promotion-evidence-root --output creates the plan, publishes it, and atomically writes its receipt JSON to --output.
- train-model and run-pipeline add optional paired --experiment-plan-receipt and --promotion-evidence-root arguments, forwarding them unchanged to train.main.
- verify-comparison adds optional --promotion-evidence-root; when provided it constructs PromotionEvidenceStore and passes it to comparison. Omission preserves only the no-receipt legacy comparison path.

- [ ] **Step 1: Write failing CLI tests**

~~~python
def test_create_experiment_plan_cli_publishes_receipt_atomically(monkeypatch, tmp_path) -> None:
    store, _ = _store_with_fake_gcs(root="gs://evidence/promotion-evidence")
    monkeypatch.setattr(cli, "PromotionEvidenceStore", lambda root: store)

    result = CliRunner().invoke(cli.app, [
        "create-experiment-plan", "--hypothesis-id", "issue-466-h1",
        "--control-id", "control", "--candidate-id", "candidate",
        "--promotion-evidence-root", "gs://evidence/promotion-evidence",
        "--output", str(tmp_path / "plan-receipt.json"),
    ])

    assert result.exit_code == 0
    assert ExperimentPlanReceipt.model_validate_json((tmp_path / "plan-receipt.json").read_text())
~~~

Add parameterized tests for supplying only one training option, for a comparison with receipts but no root, and for a backend error containing a synthetic secret. Assert Typer returns non-zero, writes no requested output, and does not echo the secret.

- [ ] **Step 2: Run CLI tests to verify they fail**

Run: uv run python -m pytest tests/test_cli.py -k "experiment_plan or promotion_evidence or verify_comparison" -v

Expected: FAIL because the command and options are absent.

- [ ] **Step 3: Implement additive CLI wiring and safe errors**

~~~python
@app.command("create-experiment-plan")
def create_experiment_plan_command(
    hypothesis_id: str = typer.Option(..., "--hypothesis-id"),
    control_id: str = typer.Option(..., "--control-id"),
    candidate_id: str = typer.Option(..., "--candidate-id"),
    promotion_evidence_root: str = typer.Option(..., "--promotion-evidence-root"),
    output: Path = typer.Option(..., "--output"),
) -> None:
    plan = create_experiment_plan(hypothesis_id=hypothesis_id, control_id=control_id, candidate_ids=(candidate_id,))
    receipt = PromotionEvidenceStore(promotion_evidence_root).publish_plan(plan)
    write_manifest_atomic(receipt, output)
    typer.echo(receipt.model_dump_json())
~~~

Use the existing CLI style for ComparisonValidationError: show the exception type and a stable Korean failure message, not backend text. Do not add a default root, a new environment variable, or registry promotion behavior. Keep current CLI calls byte-for-byte compatible when none of the new options is supplied.

- [ ] **Step 4: Update the guide from target design to implemented interface**

Replace the #466, 구현 대상 label with its committed behavior. Add the exact three command invocations, explain that receipt JSON is an untrusted transport envelope revalidated against GCS, document legacy no-receipt hold, and state that no Airflow argument or Model Registry alias behavior changes in this PR. Do not add a separate application spec.

- [ ] **Step 5: Run targeted and full verification**

Run:

~~~bash
uv run python -m pytest tests/test_pipeline_promotion_evidence.py tests/test_pipeline_train.py tests/test_training_comparison.py tests/test_pipeline_experiment_evaluation.py tests/test_cli.py -v
uv run python -m pytest -v
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
git diff --check
~~~

Expected: all tests and Ruff pass; git diff --check produces no output. If a test fails, stop at its first failure and use superpowers:systematic-debugging before changing production code.

- [ ] **Step 6: Commit CLI/docs and request the agreed checkpoint review**

~~~bash
git add src/cli.py tests/test_cli.py docs/guides/training-experiment-provenance.md
git commit -m "feat: expose verified promotion evidence workflow"
~~~

Request peer review with the exact commit range, policy constraints, and verification output. Address any actionable findings with regression tests before proceeding to the user handoff.

## Requirement Coverage Review

- GCS write-once root, three identities, retention, and production denial are infra #485 ownership; Tasks 1–5 consume and test application-visible precondition/receipt behavior without recreating Terraform or IAM.
- time_created/generation/metageneration/SHA checks, late-plan rejection, metric run window, and model/split binding are implemented in Tasks 1–3.
- Active-run held-out test ROC-AUC and no raw LLM/JSON metric injection are implemented in Tasks 2 and 4.
- Exactly 30 paired seeds and the approved t-interval verdict rule are preserved and revalidated in Task 4.
- Legacy parsing plus automatic hold, no production mutation, and no Airflow scope expansion are preserved by Tasks 2–5.

## Self-Review

- Placeholder scan: no deferred markers or generic error-handling steps remain; every task names its files, interfaces, failing test, command, implementation shape, and commit.
- Type consistency: ExperimentPlanReceipt flows from store to split manifest to comparison to paired evidence; HeldOutMetricReceipt flows from train to comparison to evaluator; PromotionEvidenceStore is the only GCS reader/writer used by application code.
- Scope check: GCS/IAM enforcement, Airflow scheduling, registry alias mutation, and production approval stay outside this plan. The five tasks form one serial application contract because each later task consumes the receipt model created by the prior task.
