# 학습 실험 provenance 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (- [ ]) syntax for tracking.

**Goal:** 학습 CSV·조립 조건·분할·seed를 MLflow run artifact로 보관하고, 두 run의 공정 비교 가능 여부를 application이 fail-closed로 검증한다.

**Architecture:** training_provenance는 JSON 계약, SHA-256, sidecar 원자 게시, seed 해석을 소유한다. Feast assembly와 training은 이 계약의 producer이고, training_comparison은 MLflow에서 producer artifact를 다시 내려받아 검증하는 consumer다. Typer CLI는 application 경계의 인자 전달과 검증 실패 exit code만 담당한다.

**Tech Stack:** Python 3.11, Pydantic v2, pandas, scikit-learn, MLflow, Typer, google-cloud-storage, pytest, Ruff.

## Global Constraints

- 변경 범위는 Autoresearch application #423뿐이다. Autoresearch-infra#464, Autoresearch-airflow, src/tracking/promote.py는 수정하지 않는다.
- Python 새 모듈은 파이프라인상 책임·비책임을 포함한 module docstring, 모든 함수의 타입 힌트, Pydantic v2 계약 모델을 사용한다.
- google-cloud-storage>=2.10과 pydantic>=2.6,<3는 이미 runtime 의존성이므로 dependency 파일을 수정하지 않는다.
- snapshot artifact 경로는 reproducibility/snapshot/training_dataset.csv, reproducibility/snapshot/snapshot_manifest.json이고, split artifact는 reproducibility/split/split_manifest.json이다.
- 비교 성공 artifact는 challenger run의 reproducibility/comparisons/<comparison-id>.json이다. Model Registry tag는 검색용이며 검증 근거가 아니다.
- run-pipeline은 검증된 snapshot sidecar를 요구한다. 직접 train-model은 sidecar가 없을 때 기존 학습 동작을 유지하지만 verified comparison 입력이 될 수 없다.
- 새 split_seed, model_seed, sampler_seed 중 하나를 지정하면 세 값을 모두 지정해야 하며 random_state와 함께 지정하면 ValueError다. 세 새 값이 모두 없으면 기존 random_state, 그 다음 config.data.random_state를 세 effective seed에 적용한다.
- 비교기는 snapshot CSV SHA-256, snapshot manifest 파일 SHA-256, train/validation/test row count·membership SHA-256, 세 effective seed만 equality 조건으로 삼는다. final feature columns와 hyperparameter는 기록만 한다.
- credential, signed URL, 전체 환경 변수를 manifest·오류·테스트 fixture에 쓰지 않는다.

## File Structure

| 파일 | 책임 |
| --- | --- |
| src/pipeline/training_provenance.py | snapshot/split/comparison Pydantic 계약, canonical JSON·SHA-256, sidecar 원자 쓰기, snapshot 검증, seed 해석 |
| src/pipeline/build_training_dataset.py | GCS registry generation을 local file로 pin한 Feast assembly와 snapshot sidecar producer |
| src/pipeline/train.py | 검증된 snapshot 소비, 세 seed로 분할·학습·downsampling, split sidecar 및 MLflow reproducibility artifact producer |
| src/pipeline/training_comparison.py | MLflow run artifact download·재검증·동등성 검사·comparison artifact publish |
| src/cli.py | 세 seed option, run-pipeline의 snapshot 요구, verify-comparison Typer command 배선 |
| tests/test_training_provenance.py | 계약·hash·sidecar·seed 순수 단위 테스트 |
| tests/test_build_training_dataset_feast_path.py | registry pinning과 assembly producer 테스트 |
| tests/test_pipeline_train.py | training producer, artifact, registry tag, legacy 회귀 테스트 |
| tests/test_training_comparison.py | MLflow artifact 기반 비교 성공·실패 테스트 |
| tests/test_cli.py | CLI 인자 전달과 fail-closed command 경계 테스트 |

---

### Task 1: Provenance 계약과 순수 검증 유틸리티

**Files:**
- Create: src/pipeline/training_provenance.py
- Create: tests/test_training_provenance.py

**Interfaces:**
- Produces: TrainingSnapshotManifest, TrainingSplitManifest, TrainingComparisonManifest, TrainingSeeds, RegistryProvenance, ProvenanceValidationError.
- Produces: snapshot_manifest_path(Path) -> Path, split_manifest_path(Path) -> Path, sha256_file(Path) -> str, build_snapshot_manifest(...), load_training_snapshot_manifest(Path), build_split_manifest(...), resolve_training_seeds(...), write_manifest_atomic(BaseModel, Path).
- Consumed by: Tasks 2–5.

- [ ] **Step 1: Write the failing contract tests**

~~~python
def test_snapshot_manifest_rejects_tampered_csv(tmp_path: Path) -> None:
    dataset_path = tmp_path / "training_dataset.csv"
    pd.DataFrame({"views": [1, 2], "clicked": [0, 1]}).to_csv(dataset_path, index=False)
    manifest = build_snapshot_manifest(
        dataset_path=dataset_path,
        events_start_date=date(2026, 7, 1),
        events_end_date=date(2026, 7, 30),
        feature_service="ctr_training_v1",
        registry=RegistryProvenance(
            uri="gs://bucket/registry.db", generation="7", sha256="a" * 64
        ),
        code_archive_sha=None,
    )
    write_manifest_atomic(manifest, snapshot_manifest_path(dataset_path))
    assert load_training_snapshot_manifest(dataset_path).dataset_sha256 == manifest.dataset_sha256

    dataset_path.write_text("views,clicked\n999,0\n", encoding="utf-8")
    with pytest.raises(ProvenanceValidationError, match="dataset_sha256"):
        load_training_snapshot_manifest(dataset_path)


def test_resolve_training_seeds_requires_complete_explicit_triplet() -> None:
    assert resolve_training_seeds(
        random_state=None, split_seed=None, model_seed=None, sampler_seed=None, config_seed=42
    ) == TrainingSeeds(split_seed=42, model_seed=42, sampler_seed=42)
    with pytest.raises(ValueError, match="모두 지정"):
        resolve_training_seeds(
            random_state=None, split_seed=1, model_seed=None, sampler_seed=3, config_seed=42
        )
~~~

Also test schema column order·dtype tampering, malformed/old manifest, deterministic membership hash, different split positions, and random_state plus explicit seed conflict.

- [ ] **Step 2: Run the new tests and verify they fail because the module does not exist**

Run: uv run python -m pytest tests/test_training_provenance.py -v

Expected: FAIL during collection with ModuleNotFoundError for src.pipeline.training_provenance.

- [ ] **Step 3: Implement the contract module**

Use ConfigDict(extra="forbid", allow_inf_nan=False), Literal version values, and a 64-character lower-case SHA-256 pattern. schema hash is the SHA-256 of ordered [{"name": name, "dtype": str(dtype)}] canonical JSON using separators=(",", ":"). membership hash is the SHA-256 of sorted source row positions encoded the same way. Model fields include:

~~~python
class TrainingSeeds(BaseModel):
    model_config = ConfigDict(frozen=True, extra="forbid")
    split_seed: int
    model_seed: int
    sampler_seed: int


def resolve_training_seeds(
    *, random_state: int | None, split_seed: int | None,
    model_seed: int | None, sampler_seed: int | None, config_seed: int,
) -> TrainingSeeds:
    explicit = (split_seed, model_seed, sampler_seed)
    if any(seed is not None for seed in explicit):
        if random_state is not None or any(seed is None for seed in explicit):
            raise ValueError("split_seed, model_seed, sampler_seed를 모두 지정해야 합니다")
        return TrainingSeeds(split_seed=split_seed, model_seed=model_seed, sampler_seed=sampler_seed)
    seed = random_state if random_state is not None else config_seed
    return TrainingSeeds(split_seed=seed, model_seed=seed, sampler_seed=seed)
~~~

TrainingSnapshotManifest records CSV byte hash, schema hash, row count, ordered column schemas, UTC creation time, KST date bounds, Feast source/service, registry URI/generation/hash, and optional code archive SHA. TrainingSplitManifest records snapshot hashes, the three seeds, split sizes, each membership row count/hash, and final feature list/hash. TrainingComparisonManifest records both run IDs, both snapshot/split manifest byte hashes, both feature list/hash, UTC validated time, deterministic comparison ID, and validation_status="verified".

write_manifest_atomic uses NamedTemporaryFile, flush, os.fsync, os.replace, and finally cleanup. load validates Pydantic JSON then current byte hash, schema hash, and row count; every failure raises ProvenanceValidationError with target path and expected/actual safe values only.

- [ ] **Step 4: Run the provenance unit tests and verify they pass**

Run: uv run python -m pytest tests/test_training_provenance.py -v

Expected: PASS; tampered CSV, malformed manifest, schema mismatch, and seed conflict are fail-closed.

- [ ] **Step 5: Commit the contract layer**

~~~bash
git add src/pipeline/training_provenance.py tests/test_training_provenance.py
git commit -m "feat: 학습 provenance 계약 추가"
~~~

### Task 2: Feast assembly registry pinning과 snapshot sidecar producer

**Files:**
- Modify: src/pipeline/build_training_dataset.py:1-31, 193-257, 383-402
- Modify: tests/test_build_training_dataset_feast_path.py:43-112

**Interfaces:**
- Consumes: Task 1 RegistryProvenance, build_snapshot_manifest, snapshot_manifest_path, write_manifest_atomic.
- Produces: output CSV plus <output>.snapshot.json; manifest registry fields match the exact local registry file passed to FeatureStore.
- Consumed by: Task 3.

- [ ] **Step 1: Write the failing assembly tests**

Extend _fake_env with a fake pinned registry download and collect the registry_path passed to build_offline_feature_store.

~~~python
def test_assemble_pins_registry_and_writes_snapshot(tmp_path, monkeypatch) -> None:
    seen: dict[str, str] = {}
    monkeypatch.setattr(
        btd, "_download_pinned_registry",
        lambda uri, destination: destination.write_bytes(b"registry-v7") or RegistryProvenance(
            uri=uri, generation="7", sha256=hashlib.sha256(b"registry-v7").hexdigest()
        ),
    )
    monkeypatch.setattr(
        feast_retrieval, "build_offline_feature_store",
        lambda registry_path, **kwargs: seen.setdefault("registry_path", registry_path) or object(),
    )
    btd._assemble_via_feast(str(tmp_path / "out.csv"), "2026-07-01", "2026-07-30")
    assert seen["registry_path"].endswith("registry.db")
    assert not seen["registry_path"].startswith("gs://")
    assert snapshot_manifest_path(tmp_path / "out.csv").is_file()


def test_registry_download_failure_creates_no_dataset_or_sidecar(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr(
        btd, "_download_pinned_registry",
        lambda *_: (_ for _ in ()).throw(ProvenanceValidationError("registry download failed")),
    )
    with pytest.raises(ProvenanceValidationError, match="registry"):
        btd._assemble_via_feast(str(tmp_path / "out.csv"), "2026-07-01", "2026-07-30")
    assert not (tmp_path / "out.csv").exists()
    assert not snapshot_manifest_path(tmp_path / "out.csv").exists()
~~~

Test _download_pinned_registry separately with fake storage client: reload reads generation, bucket.blob(name, generation=...) creates the second Blob, and only that Blob downloads. Reject malformed gs URI and missing generation.

- [ ] **Step 2: Run the focused assembly tests and verify they fail**

Run: uv run python -m pytest tests/test_build_training_dataset_feast_path.py -v

Expected: FAIL because the pinning helper and snapshot sidecar do not exist.

- [ ] **Step 3: Implement pinned registry download and atomic dataset publication**

Implement _download_pinned_registry(uri: str, destination: Path, client: storage.Client | None = None) -> RegistryProvenance. Parse exact bucket/object with urlparse, call blob.reload(), require generation, create a generation-specific Blob, download it, then calculate its byte hash. Do not include credentials, signed URL, or environment dumps in failure text.

Change _assemble_via_feast to use TemporaryDirectory(prefix="feast_assemble_") for registry.db and online.db, pass only local registry.db to build_offline_feature_store, and retain it until retrieve_training_features returns. Write selected features to a temporary CSV in the output directory, build its manifest using DEFAULT_SERVICE, exact date arguments, RegistryProvenance, and optional CODE_ARCHIVE_SHA. Atomically replace the CSV first, then atomically publish the manifest last. Update the module responsibility docstring accordingly.

- [ ] **Step 4: Run focused assembly and environment tests**

Run: uv run python -m pytest tests/test_build_training_dataset_feast_path.py tests/test_build_training_dataset_env_check_feast.py -v

Expected: PASS; successful assembly uses a local pinned registry and failure publishes neither file.

- [ ] **Step 5: Commit the assembly producer**

~~~bash
git add src/pipeline/build_training_dataset.py tests/test_build_training_dataset_feast_path.py
git commit -m "feat: Feast 학습 snapshot provenance 기록"
~~~

### Task 3: Training snapshot 소비·split provenance·MLflow artifact producer

**Files:**
- Modify: src/pipeline/train.py:1-31, 299-703
- Modify: tests/test_pipeline_train.py:1-230, 560-760

**Interfaces:**
- Consumes: Task 1 contracts and Task 2 sidecar.
- Produces: train.main(..., split_seed, model_seed, sampler_seed, require_snapshot), local <test-set>.split.json, canonical MLflow artifacts, snapshot_sha256 and split_manifest_sha256 Registry tags.
- Consumed by: Tasks 4–5.

- [ ] **Step 1: Write failing training tests**

Create _prepared_verified_dataset by calling Task 1 helpers around the current synthetic fixture, then test verified artifacts, stale snapshot, invalid seed, legacy random_state, and Registry tags.

~~~python
def test_train_logs_verified_snapshot_and_split_artifacts(tmp_path, monkeypatch) -> None:
    config_path, dataset_path, tracking_uri = _prepared_verified_dataset(tmp_path, monkeypatch)
    outcome = train.main(
        config_path=str(config_path), data_path=str(dataset_path),
        model_output=str(tmp_path / "model.joblib"), test_set_output=str(tmp_path / "test_set.csv"),
        feature_columns_output=str(tmp_path / "features.json"),
        categorical_columns_output=str(tmp_path / "categories.json"),
        split_seed=11, model_seed=12, sampler_seed=13, require_snapshot=True,
    )
    client = MlflowClient(tracking_uri=tracking_uri)
    assert _artifact_paths(client, outcome.run_id, "reproducibility") == {
        "reproducibility/snapshot/training_dataset.csv",
        "reproducibility/snapshot/snapshot_manifest.json",
        "reproducibility/split/split_manifest.json",
    }
    split = TrainingSplitManifest.model_validate_json(
        split_manifest_path(tmp_path / "test_set.csv").read_text(encoding="utf-8")
    )
    assert (split.split_seed, split.model_seed, split.sampler_seed) == (11, 12, 13)


def test_train_rejects_stale_snapshot_before_model_fit(tmp_path, monkeypatch) -> None:
    config_path, dataset_path, _ = _prepared_verified_dataset(tmp_path, monkeypatch)
    dataset_path.write_text("changed", encoding="utf-8")
    fit = MagicMock()
    monkeypatch.setattr(train.LGBMModel, "fit", fit)
    with pytest.raises(ProvenanceValidationError, match="dataset_sha256"):
        _run_train_with_snapshot(config_path, dataset_path)
    fit.assert_not_called()
~~~

- [ ] **Step 2: Run the focused training tests and verify they fail**

Run: uv run python -m pytest tests/test_pipeline_train.py -v

Expected: FAIL because train.main lacks explicit seed/require_snapshot arguments and reproducibility artifacts.

- [ ] **Step 3: Implement verified training without breaking direct training**

Resolve config and normalized data_path before mlflow.start_run. If sidecar exists, load_training_snapshot_manifest must validate it; if require_snapshot is true and it does not exist, raise ProvenanceValidationError before test set writes or model fitting. No-sidecar direct train continues its existing behavior.

Use effective seeds as follows:

~~~python
effective = resolve_training_seeds(
    random_state=random_state, split_seed=split_seed, model_seed=model_seed,
    sampler_seed=sampler_seed, config_seed=int(config["data"]["random_state"]),
)
source_positions = np.arange(len(dataset))
train_val_positions, test_positions = train_test_split(
    source_positions, test_size=test_size, random_state=effective.split_seed,
    stratify=dataset[LABEL_COLUMN],
)
train_positions, val_positions = train_test_split(
    train_val_positions, test_size=val_size / (1 - test_size),
    random_state=effective.split_seed,
    stratify=dataset.iloc[train_val_positions][LABEL_COLUMN],
)
train_df = dataset.iloc[train_positions]
val_df = dataset.iloc[val_positions]
test_df = dataset.iloc[test_positions]
~~~

Pass sampler_seed only to downsample_negatives and model_seed only to LGBMModel. Preserve legacy random_state parameter logging and always log the three effective seed parameters. After feature columns are final and before model.fit, create the split manifest, write <test-set>.split.json atomically, and log the input CSV, snapshot manifest, and split manifest under the canonical artifact names. Stage copies with canonical filenames in a temporary directory so custom local input filenames never change MLflow artifact paths. Do not swallow artifact upload errors. Add snapshot and split manifest byte hashes to PendingRegistration tags only for verified runs.

- [ ] **Step 4: Run focused training tests and seed-sweep regression**

Run: uv run python -m pytest tests/test_pipeline_train.py tests/test_pipeline_seed_sweep.py -v

Expected: PASS; stale data and invalid seeds fail before fit, verified runs log all artifacts, and #407 seed sweep remains legacy-compatible.

- [ ] **Step 5: Commit the training producer**

~~~bash
git add src/pipeline/train.py tests/test_pipeline_train.py
git commit -m "feat: 학습 split provenance artifact 기록"
~~~

### Task 4: CLI seed surface와 run-pipeline snapshot requirement

**Files:**
- Modify: src/cli.py:1-18, 70-285, 510-580
- Modify: tests/test_cli.py:40-235, 560-650

**Interfaces:**
- Consumes: Task 3 train.main signature.
- Produces: train-model and run-pipeline split/model/sampler options; run-pipeline passes require_snapshot=True.
- Preserves: sweep-seeds uses its current random_state legacy path.

- [ ] **Step 1: Write failing CLI forwarding tests**

~~~python
def test_run_pipeline_requires_verified_snapshot_and_forwards_seed_triplet(monkeypatch) -> None:
    seen: dict[str, object] = {}
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setattr(cli.build_training_dataset, "main", MagicMock())
    monkeypatch.setattr(
        cli.train, "main", lambda **kwargs: seen.update(kwargs) or _pipeline_outcome()
    )
    monkeypatch.setattr(cli.evaluate, "main", MagicMock())
    monkeypatch.setattr(cli.train, "register_pending_model", MagicMock())
    cli.run_pipeline(
        events_start_date="2026-07-01", events_end_date="2026-07-30",
        split_seed=11, model_seed=12, sampler_seed=13,
    )
    assert seen["require_snapshot"] is True
    assert (seen["split_seed"], seen["model_seed"], seen["sampler_seed"]) == (11, 12, 13)
~~~

Also verify train_model forwards the same options and leave sweep-seeds random_state mock assertions intact.

- [ ] **Step 2: Run the CLI tests and verify they fail**

Run: uv run python -m pytest tests/test_cli.py -v

Expected: FAIL with absent split_seed, model_seed, sampler_seed, or require_snapshot forwarding.

- [ ] **Step 3: Implement CLI-only boundary changes**

Add three optional typer.Option arguments to train_model and run_pipeline, forward them unchanged to train.main, and set require_snapshot=True only in run_pipeline. Do not add these options to sweep_seeds. Update CLI module docstring and option help to distinguish split, model, and sampler seeds.

- [ ] **Step 4: Run CLI and training integration tests**

Run: uv run python -m pytest tests/test_cli.py tests/test_pipeline_train.py -v

Expected: PASS; pipeline requires verified snapshot and direct/sweep legacy calls remain valid.

- [ ] **Step 5: Commit the CLI training interface**

~~~bash
git add src/cli.py tests/test_cli.py
git commit -m "feat: 공정 비교용 학습 seed 인자 추가"
~~~

### Task 5: MLflow artifact comparison and verify-comparison command

**Files:**
- Create: src/pipeline/training_comparison.py
- Create: tests/test_training_comparison.py
- Modify: src/cli.py:1-18, 580-640
- Modify: tests/test_cli.py:1-40, 650-720

**Interfaces:**
- Consumes: Task 1 models/validators and Task 3 canonical artifact layout.
- Produces: verify_training_comparison(baseline_run_id: str, challenger_run_id: str, output_path: Path) -> TrainingComparisonManifest and Typer verify-comparison.
- Failure contract: missing, malformed, tampered, or unequal artifact raises ComparisonValidationError before creating output_path or invoking MlflowClient.log_artifact.

- [ ] **Step 1: Write failing local-MLflow comparison tests**

The fixture creates two MLflow runs with Task 1 manifest helpers and logs the canonical paths. Different feature columns are permitted.

~~~python
def test_verify_training_comparison_writes_output_and_challenger_artifact(tmp_path) -> None:
    baseline_run = _log_verified_run(tmp_path, split_seed=42, feature_columns=["views"])
    challenger_run = _log_verified_run(
        tmp_path, split_seed=42, feature_columns=["views", "new_feature"]
    )
    output = tmp_path / "comparison.json"

    result = verify_training_comparison(baseline_run, challenger_run, output)

    assert result.validation_status == "verified"
    assert output.is_file()
    assert _artifact_exists(
        challenger_run, f"reproducibility/comparisons/{result.comparison_id}.json"
    )


def test_seed_mismatch_has_no_output_or_challenger_upload(tmp_path) -> None:
    baseline_run = _log_verified_run(tmp_path, split_seed=42)
    challenger_run = _log_verified_run(tmp_path, split_seed=43)
    output = tmp_path / "comparison.json"
    with pytest.raises(ComparisonValidationError, match="split_seed"):
        verify_training_comparison(baseline_run, challenger_run, output)
    assert not output.exists()
    assert _comparison_artifacts(challenger_run) == []
~~~

Add independent cases for missing artifact, tampered CSV, manifest byte hash mismatch, each split membership/row-count mismatch, model_seed mismatch, sampler_seed mismatch, and a CLI error that does not echo a synthetic secret.

- [ ] **Step 2: Run the comparison tests and verify they fail**

Run: uv run python -m pytest tests/test_training_comparison.py tests/test_cli.py -v

Expected: FAIL during collection because training_comparison and verify-comparison do not exist.

- [ ] **Step 3: Implement artifact verification, publish, and CLI wiring**

training_comparison owns comparison validation, not model training, #407 significance, champion promotion, or infrastructure lifecycle. Download each canonical artifact through mlflow.artifacts.download_artifacts with a runs:/ URI and revalidate CSV/manifest through Task 1 before comparing.

~~~python
def _assert_equal(label: str, baseline: object, challenger: object) -> None:
    if baseline != challenger:
        raise ComparisonValidationError(f"{label} differs between baseline and challenger")


def verify_training_comparison(
    baseline_run_id: str, challenger_run_id: str, output_path: Path,
) -> TrainingComparisonManifest:
    baseline = _load_verified_run_artifacts(baseline_run_id)
    challenger = _load_verified_run_artifacts(challenger_run_id)
    _assert_equal("snapshot_sha256", baseline.snapshot.dataset_sha256, challenger.snapshot.dataset_sha256)
    _assert_equal("split_seed", baseline.split.split_seed, challenger.split.split_seed)
    # Compare all remaining required equality fields before any output or upload.
    result = _build_comparison_manifest(baseline, challenger)
    _publish_verified_comparison(result, challenger_run_id, output_path)
    return result
~~~

comparison_id is the first 16 lower-case SHA-256 characters of baseline run ID, challenger run ID, snapshot SHA, and split manifest SHA separated by NUL. The manifest contains both run IDs, both snapshot/split manifest byte hashes, both final feature list/hash, UTC validated time, and validation_status="verified". Create the JSON in a hidden temp file; after all equality checks, use MlflowClient.log_artifact(challenger_run_id, temp_path, artifact_path="reproducibility/comparisons"), then atomically replace the requested output. Validation failure therefore changes neither destination.

Add an app.command named verify-comparison with required --baseline-run-id, --challenger-run-id, --output. It prints the verified JSON on stdout and maps ComparisonValidationError to a safe stderr diagnostic and typer.Exit(code=1). It must not call promote, seed_sweep, Airflow, or Model Registry alias APIs.

- [ ] **Step 4: Run comparison, CLI, and provenance suites**

Run: uv run python -m pytest tests/test_training_provenance.py tests/test_pipeline_train.py tests/test_training_comparison.py tests/test_cli.py -v

Expected: PASS; equal snapshot/split/seed runs create both destinations, and every mismatch creates neither destination.

- [ ] **Step 5: Commit comparison validation**

~~~bash
git add src/pipeline/training_comparison.py src/cli.py tests/test_training_comparison.py tests/test_cli.py
git commit -m "feat: MLflow 학습 run 공정 비교 검증 추가"
~~~

### Task 6: Documentation reconciliation and final verification

**Files:**
- Modify: docs/guides/training-experiment-provenance.md
- Move after successful implementation: docs/plans/2026-07-31-training-experiment-provenance.md to docs/archive/plans/2026-07-31-training-experiment-provenance.md
- Modify: tests/test_cli.py

**Interfaces:**
- Consumes: final Task 1–5 contracts.
- Produces: current application design guide and archived completed implementation plan.

- [ ] **Step 1: Write the failing command-surface test**

~~~python
def test_verify_comparison_help_exposes_required_options() -> None:
    result = CliRunner().invoke(cli.app, ["verify-comparison", "--help"])
    assert result.exit_code == 0
    assert "--baseline-run-id" in result.output
    assert "--challenger-run-id" in result.output
    assert "--output" in result.output
~~~

- [ ] **Step 2: Run the help test and verify the command surface**

Run: uv run python -m pytest tests/test_cli.py::test_verify_comparison_help_exposes_required_options -v

Expected: PASS after Task 5. If it fails, return to Task 5; do not alter the guide to describe a missing option.

- [ ] **Step 3: Reconcile the guide with executable contracts**

Compare the guide’s CLI example, artifact table, explicit seed semantics, and repository boundary table to Tasks 1–5. Modify only statements that differ. Do not add Airflow deployment instructions, registry champion gates, or a canonical GCS snapshot registry implementation.

- [ ] **Step 4: Run final verification**

Run: uv run python -m pytest

Expected: PASS with no failures.

Run: uv run --no-sync ruff check agent_orchestration autoresearch tests tools

Expected: exit 0 with no lint violations.

Run: git diff --check

Expected: exit 0 with no whitespace errors.

- [ ] **Step 5: Archive plan and commit final documentation**

~~~bash
git mv docs/plans/2026-07-31-training-experiment-provenance.md \
  docs/archive/plans/2026-07-31-training-experiment-provenance.md
git add docs/guides/training-experiment-provenance.md docs/archive/plans/2026-07-31-training-experiment-provenance.md
git commit -m "docs: 학습 provenance 구현 계획 아카이브"
~~~
