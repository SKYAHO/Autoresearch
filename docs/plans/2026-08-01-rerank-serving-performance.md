# Rerank Serving 성능 벤치마크 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** dev GKE의 Feast-backed POST /rerank을 단계별로 계측하고, 고정 fixture와 격리된 k6 Job으로 개선 전·후를 같은 조건에서 비교한다.

**Architecture:** ServingFeatureBuilder는 기존 후보 목록 반환 계약을 보존하면서 HTTP route가 소비할 timing 결과를 추가한다. FastAPI가 두 Feast batch read, 피처 조립, 모델 예측, 응답 생성을 fixed-label Prometheus metric으로 기록한다. fixture는 BigQuery source table의 정확한 loadtest entity만 갱신하고 기존 Airflow materialize DAG가 Redis online store에 반영한다. k6 Job은 loadtest namespace에서 serving ClusterIP만 호출한다.

**Tech Stack:** Python 3.11+, FastAPI, prometheus-client, Feast 0.64, BigQuery, Airflow, GKE, Kubernetes Job/NetworkPolicy/RBAC, k6, GitHub Actions, Terraform, Prometheus.

## Global Constraints

- Public RerankRequest/RerankResponse, input video_ids order, current 422/503/500 semantics do not change.
- rerank_duration_seconds remains handler end-to-end duration after validation.
- rerank_phase_duration_seconds has exactly five phase labels: feature_read_first, feature_read_second, feature_assemble, model_predict, response_build.
- rerank_outcomes_total has exactly success, feature_error, prediction_error, unavailable. rerank_in_flight has no labels.
- Prometheus labels never contain user ID, video ID, model ID, benchmark run ID, or exception text. No Uvicorn multi-worker or multiprocess Prometheus change.
- Fixture IDs are loadtest-user-001 and loadtest-video-001 through loadtest-video-200. WRITE_TRUNCATE, CREATE OR REPLACE, direct Redis write, and random dummy seed reuse are forbidden.
- After fixture DML, manually trigger feast_online_store_materialize and wait for job_summary.status=succeeded. Refresh UserDynamic timestamp before baseline and optimized runs.
- For candidates 24 and 200, execute VU 1, 2, 4, 8 as independent Jobs. Advance only after preceding measured error rate is strictly under 1 percent. Warmup is 60 seconds and excluded; measured duration is 5 minutes.
- Only call http://autoresearch-serving.autoresearch.svc.cluster.local:8000. No replica change, Pod kill, LoadBalancer, or Ingress.
- Comparison pairs have the same candidates, VU, fixture, model version, pod requests/limits, warmup, duration. Serving image digest and Git SHA identify the code difference.
- Autoresearch owns code, fixture, k6, workflow, runbook, report. Autoresearch-infra owns namespace, KSA, RBAC, NetworkPolicy, WIF, dashboard. They use separate issue/branch/PR tracks.

---

## Security and repository tracks

| Track | Repository | Deliverable | Gate |
| --- | --- | --- | --- |
| A | Autoresearch | metrics, fixture, k6, workflow, runbook/report | unit/API/k6 syntax tests |
| B | Autoresearch-infra | network, KSA/RBAC/WIF, Prometheus reader, dashboard | Terraform and policy review |

Raw k6 and Prometheus range JSON must become Actions artifacts. Do not give the load generator Prometheus access. Create two distinct OIDC identities:

1. rerank-loadtest-runner: only labeled ConfigMap, Job, Pod, and Pod log operations in loadtest. No app, monitoring, Secret, or GCP data-plane access.
2. rerank-prometheus-snapshot-reader: only get on the one Prometheus Service proxy in monitoring. No Job, Pod, Secret, ConfigMap, or write access.

Fixture DML remains a manual operation by an already-authorized BigQuery operator. No new CI BigQuery credential is introduced.

## File map

| File | Change | Responsibility |
| --- | --- | --- |
| src/serving/online_features.py | modify | timing result for two reads and assembly |
| src/serving/app.py | modify | phase/outcome/in-flight lifecycle |
| tests/test_serving_online_features.py | modify | timing preserves current contract |
| tests/test_serving_api.py | modify | HTTP status and metric deltas |
| autoresearch/loadtest/rerank_fixture.py | create | deterministic rows and DML renderer |
| scripts/provision_rerank_loadtest_fixture.py | create | dry-run-default provisioner |
| tests/test_rerank_loadtest_fixture.py | create | fixture, k6, Job, runbook contracts |
| loadtest/rerank.js | create | canary, warmup, measurement |
| deploy/loadtest/rerank-k6-job.yaml | create | hardened one-shot Job |
| .github/workflows/rerank-loadtest.yml | create | manual Job runner and artifacts |
| docs/runbooks/rerank-loadtest.md | create | materialize/report procedure |
| docs/README.md | modify | serving documentation index |

## Task 1: Add internal feature build timing

**Files:**

- Modify: src/serving/online_features.py:34-175
- Modify: tests/test_serving_online_features.py:1-160

**Interfaces:**

~~~python
@dataclass(frozen=True, slots=True)
class FeatureBuildTimings:
    first_read_seconds: float
    second_read_seconds: float
    assemble_seconds: float

@dataclass(frozen=True, slots=True)
class TimedFeatureBuild:
    candidates: list[CandidateVideo]
    timings: FeatureBuildTimings

def ServingFeatureBuilder.build_with_timings(
    self,
    *,
    user_id: str,
    video_ids: Sequence[str],
    feature_columns: Sequence[str],
) -> TimedFeatureBuild: ...
~~~

Existing build delegates to this typed method and returns only candidates.

- [ ] **Step 1: Write a failing timing test.**

~~~python
def test_build_with_timings_preserves_candidate_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    reader = FakeReader(responses=[_first_response(), _similarity_response()])
    clocks = iter((0.00, 0.02, 0.02, 0.12, 0.12, 0.17, 0.17, 0.37, 0.37, 0.52))
    monkeypatch.setattr(online_features, "perf_counter", lambda: next(clocks))

    result = ServingFeatureBuilder(reader=reader).build_with_timings(
        user_id="user-1",
        video_ids=["video-a", "video-b", "video-c"],
        feature_columns=MODEL_FEATURE_COLUMNS,
    )

    assert [item.video_id for item in result.candidates] == [
        "video-a", "video-b", "video-c"
    ]
    assert result.timings == FeatureBuildTimings(0.10, 0.20, 0.22)
    assert len(reader.calls) == 2
~~~

- [ ] **Step 2: Verify the test fails.**

Run:

~~~bash
uv run python -m pytest tests/test_serving_online_features.py::test_build_with_timings_preserves_candidate_order -v
~~~

Expected: FAIL because build_with_timings is absent.

- [ ] **Step 3: Implement the typed path.**

Import perf_counter. Add the two frozen dataclasses after FeatureRetrievalError. Time reader.read once for first_read_seconds and once for second_read_seconds. Add three assembly intervals: validation plus first entities; first keyed rows plus category dedup plus second entities; candidate construction. Do not subtract reads from an end-to-end time because that can create rounding error. A read failure retains the existing typed error; Task 2 counts it as an outcome.

- [ ] **Step 4: Run focused regression.**

Run:

~~~bash
uv run python -m pytest tests/test_serving_online_features.py -v
~~~

Expected: PASS; two batch reads, category dedup, typed defaults, 21 features, and order still pass.

- [ ] **Step 5: Commit.**

~~~bash
git add src/serving/online_features.py tests/test_serving_online_features.py
git commit -m "feat: 리랭킹 피처 조립 단계 시간 추가"
~~~

## Task 2: Add fixed Prometheus route metrics

**Files:**

- Modify: src/serving/app.py:42-193
- Modify: tests/test_serving_api.py:1-340

**Interfaces:**

~~~python
RERANK_PHASE_DURATION = Histogram(
    "rerank_phase_duration_seconds",
    "Duration of fixed reranking request phases.",
    ["phase"],
)
RERANK_OUTCOMES = Counter(
    "rerank_outcomes",
    "Reranking request outcomes after request validation.",
    ["outcome"],
)
RERANK_IN_FLIGHT = Gauge(
    "rerank_in_flight",
    "Reranking requests currently executing after request validation.",
)
~~~

FakeFeatureBuilder implements build_with_timings and returns prior candidates with FeatureBuildTimings(0.01, 0.02, 0.03). Failing fakes keep their existing typed exception.

- [ ] **Step 1: Write failing API metric tests.**

~~~python
def _sample_value(
    client: TestClient, name: str, labels: Mapping[str, str]
) -> float:
    for family in text_string_to_metric_families(client.get("/metrics").text):
        for sample in family.samples:
            if sample.name == name and sample.labels == labels:
                return float(sample.value)
    return 0.0

def test_rerank_observes_fixed_phases_success_and_in_flight() -> None:
    app = create_app(resolved_model=_resolved_model(), feature_builder=FakeFeatureBuilder())
    with TestClient(app) as client:
        before = _sample_value(client, "rerank_outcomes_total", {"outcome": "success"})
        response = client.post(
            "/rerank", json={"user_id": "user-1", "video_ids": ["video-1"]}
        )
        after = _sample_value(client, "rerank_outcomes_total", {"outcome": "success"})
        text = client.get("/metrics").text

    assert response.status_code == 200
    assert after == before + 1
    assert _sample_value(client, "rerank_in_flight", {}) == 0
    assert "feature_read_first" in text and "response_build" in text
    assert "user-1" not in text and "video-1" not in text and "run-123" not in text
~~~

Also add one test each for feature_error/503, prediction_error/500, and unavailable/503. Each checks delta is exactly one.

- [ ] **Step 2: Verify failure.**

Run:

~~~bash
uv run python -m pytest tests/test_serving_api.py -k "fixed_phases or feature_and_prediction or unavailable" -v
~~~

Expected: FAIL because metric names and timed builder call are absent.

- [ ] **Step 3: Implement lifecycle order.**

1. Increment in-flight and decrement it exactly once in a finally clause.
2. Put readiness and all post-validation work in the existing duration timer. Unavailable increments unavailable once, then raises the unchanged 503.
3. Preserve request/video histograms. Observe the three returned values under feature_read_first, feature_read_second, feature_assemble.
4. Time rerank_with_diagnostics plus output ID length/set check as model_predict.
5. Time unseen-category logging/counting, score map, response item/response construction as response_build. Increment success only after response construction.
6. Map FeatureContractError/FeatureRetrievalError to feature_error plus the current 503. Map PredictionError to prediction_error plus the current safe 500.

Pydantic 422 happens before the route and has no outcome label. Do not create a catch-all outcome.

- [ ] **Step 4: Run regression and commit.**

Run:

~~~bash
uv run python -m pytest tests/test_serving_api.py -v
uv run python -m pytest tests/test_serving_online_features.py tests/test_serving_api.py -v
~~~

Expected: PASS.

~~~bash
git add src/serving/app.py tests/test_serving_api.py
git commit -m "feat: 리랭킹 단계별 서빙 메트릭 추가"
~~~

## Task 3: Create deterministic fixture and safe provisioner

**Files:**

- Create: autoresearch/loadtest/__init__.py
- Create: autoresearch/loadtest/rerank_fixture.py
- Create: scripts/provision_rerank_loadtest_fixture.py
- Create: tests/test_rerank_loadtest_fixture.py

**Interfaces:**

~~~python
FIXTURE_VERSION: Final[str] = "rerank-v1"
FIXTURE_USER_ID: Final[str] = "loadtest-user-001"
FIXTURE_VIDEO_IDS: Final[tuple[str, ...]]
FIXTURE_CATEGORY_IDS: Final[tuple[str, ...]] = ("10", "20", "22", "24", "25")

@dataclass(frozen=True, slots=True)
class FixtureTable:
    name: str
    entity_column: str
    rows: tuple[Mapping[str, object], ...]

def build_fixture(timestamp: datetime) -> tuple[FixtureTable, ...]: ...
def targeted_delete_sql(
    project: str, dataset: str, table: FixtureTable
) -> tuple[str, bigquery.QueryJobConfig]: ...
def targeted_insert_sql(project: str, dataset: str, table: FixtureTable) -> str: ...
~~~

- [ ] **Step 1: Write failing safety tests.**

~~~python
def test_fixture_has_exact_row_counts() -> None:
    tables = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))
    rows = {table.name: table.rows for table in tables}
    assert FIXTURE_VIDEO_IDS[0] == "loadtest-video-001"
    assert FIXTURE_VIDEO_IDS[-1] == "loadtest-video-200"
    assert {name: len(value) for name, value in rows.items()} == {
        "user_static_feature": 1,
        "user_dynamic_feature": 1,
        "video_feature": 200,
        "user_category_similarity": 5,
    }

def test_video_dml_is_exact_and_non_destructive() -> None:
    table = build_fixture(datetime(2026, 8, 1, tzinfo=UTC))[2]
    delete_sql, config = targeted_delete_sql("project-1", "feast_offline_store", table)
    insert_sql = targeted_insert_sql("project-1", "feast_offline_store", table)
    assert "video_id IN UNNEST(@video_ids)" in delete_sql
    assert config.query_parameters[0].name == "video_ids"
    assert "loadtest-video-001" in insert_sql
    assert "WRITE_TRUNCATE" not in delete_sql + insert_sql
    assert "CREATE OR REPLACE" not in delete_sql + insert_sql
~~~

Add tests that user-keyed tables delete by user_id parameter loadtest-user-001, and invalid project/dataset identifiers raise ValueError.

- [ ] **Step 2: Verify failure.**

Run:

~~~bash
uv run python -m pytest tests/test_rerank_loadtest_fixture.py -v
~~~

Expected: FAIL because autoresearch.loadtest is absent.

- [ ] **Step 3: Implement fixed data and exact DML.**

IDs are loadtest-video-001 through 200. Categories cycle 10,20,22,24,25. All rows use one UTC second-precision timestamp. Static user is 30s/engineer/medium with preferred categories 10/20/22. Dynamic values are 50 clicks, 500 views, 36000 watch seconds, 25 likes, historical category 10, total 575. Video i has duration 60+i, view count 100000+i*100, ratios 0.05/0.005, days i modulo 365, and fixed channel counts. Five similarity rows use 0.10 through 0.50.

Validate project, dataset, and the four allowed table names before composing an identifier. DELETE uses BigQuery parameters. INSERT has all source-table columns and only module-generated fixed literals. No caller-provided string is written as a literal.

- [ ] **Step 4: Implement dry-run-default CLI.**

Run:

~~~bash
uv run --no-dev --group feast python scripts/provision_rerank_loadtest_fixture.py --project "$GCP_PROJECT_ID" --dataset feast_offline_store
uv run --no-dev --group feast python scripts/provision_rerank_loadtest_fixture.py --project "$GCP_PROJECT_ID" --dataset feast_offline_store --apply
~~~

Dry run prints version, now UTC minus five minutes, and exact existing counts without DML. Apply calculates one timestamp, waits for targeted DELETE and INSERT for each table, and prints deleted/inserted counts and RFC3339 time. It states that Redis is not updated until Airflow materialization succeeds.

- [ ] **Step 5: Verify and commit.**

Run:

~~~bash
uv run python -m pytest tests/test_rerank_loadtest_fixture.py -v
uv run --no-dev --group feast python scripts/provision_rerank_loadtest_fixture.py --help
~~~

Expected: PASS.

~~~bash
git add autoresearch/loadtest scripts/provision_rerank_loadtest_fixture.py tests/test_rerank_loadtest_fixture.py
git commit -m "feat: 리랭킹 부하테스트 fixture 추가"
~~~

## Task 4: Implement k6 canary and measurement contract

**Files:**

- Create: loadtest/rerank.js
- Create: loadtest/README.md
- Modify: tests/test_rerank_loadtest_fixture.py

**Inputs:** BASE_URL, CANDIDATE_COUNT, VUS, WARMUP_SECONDS, MEASURE_SECONDS, FIXTURE_VERSION, BENCHMARK_LABEL, SERVING_IMAGE_REF, SERVING_GIT_SHA.

- [ ] **Step 1: Write failing script contract test.**

~~~python
def test_k6_script_has_warmup_and_measurement_contract() -> None:
    script = Path("loadtest/rerank.js").read_text()
    assert 'exec: "warmup"' in script
    assert 'exec: "measure"' in script
    assert "rerank_measure_duration_seconds" in script
    assert "rerank_measure_failure" in script
    assert "rate<0.01" in script
    assert "loadtest-user-001" in script
    assert "loadtest-video-200" in script
~~~

- [ ] **Step 2: Verify failure.**

Run:

~~~bash
uv run python -m pytest tests/test_rerank_loadtest_fixture.py::test_k6_script_has_warmup_and_measurement_contract -v
~~~

Expected: FAIL because the script is absent.

- [ ] **Step 3: Implement script.**

At import reject candidate values other than 24/200 and VU other than 1/2/4/8. Build 200 ordered IDs and slice requested count. setup sends 24 and 200 canaries before scenarios. Every request including warmup checks 200, exact item count/order, one non-empty model ID, and finite score.

Use constant-vus warmup and a delayed constant-vus measure scenario. Construct duration strings with String(warmupSeconds) + "s" and String(measureSeconds) + "s". Threshold rerank_measure_failure is rate<0.01. Only measurement adds duration to rerank_measure_duration_seconds, request count to rerank_measure_requests, validity to rerank_measure_failure, and status to rerank_measure_status_code. handleSummary writes one stdout JSON object with metadata and data.metrics only.

- [ ] **Step 4: Verify syntax and commit.**

Run:

~~~bash
uv run python -m pytest tests/test_rerank_loadtest_fixture.py -v
docker run --rm -v "$PWD/loadtest:/scripts:ro" grafana/k6:0.54.0 inspect /scripts/rerank.js
~~~

Expected: PASS. Resolve the k6 digest and pin that immutable image in Task 5 before merge.

~~~bash
git add loadtest/rerank.js loadtest/README.md tests/test_rerank_loadtest_fixture.py
git commit -m "test: 리랭킹 k6 요청 계약 추가"
~~~

## Task 5: Create hardened Job and manual workflow

**Files:**

- Create: deploy/loadtest/rerank-k6-job.yaml
- Create: .github/workflows/rerank-loadtest.yml
- Modify: tests/test_rerank_loadtest_fixture.py

- [ ] **Step 1: Write failing Job hardening test.**

~~~python
def test_k6_job_has_no_identity_or_token_mount() -> None:
    text = Path("deploy/loadtest/rerank-k6-job.yaml").read_text()
    assert "serviceAccountName: rerank-loadtest" in text
    assert "automountServiceAccountToken: false" in text
    assert "restartPolicy: Never" in text
    assert "allowPrivilegeEscalation: false" in text
    assert "readOnlyRootFilesystem: true" in text
    assert "REDIS_" not in text and "secretKeyRef:" not in text
~~~

- [ ] **Step 2: Verify failure.**

Run:

~~~bash
uv run python -m pytest tests/test_rerank_loadtest_fixture.py::test_k6_job_has_no_identity_or_token_mount -v
~~~

Expected: FAIL because manifest is absent.

- [ ] **Step 3: Implement one-shot Job.**

Use generateName rerank-k6-, namespace loadtest, label app.kubernetes.io/part-of=rerank-loadtest, backoffLimit 0, activeDeadlineSeconds 600, and ttlSecondsAfterFinished 86400. Its one container uses resolved grafana/k6 digest and runs k6 run /scripts/rerank.js.

Set runAsNonRoot, readOnlyRootFilesystem, allowPrivilegeEscalation false, drop ALL capabilities, RuntimeDefault seccomp, and automountServiceAccountToken false. Mount only script/settings ConfigMaps read-only. No Secret, GCP identity annotation, credential volume, DB/Redis variable, or host volume.

- [ ] **Step 4: Implement manual workflow.**

Inputs are candidate_count choice 24/200, benchmark_label baseline/optimized, fixture_version default rerank-v1, required serving_image_ref, and required serving_git_sha. The loadtest runner loops VU 1,2,4,8:

1. Authenticate OIDC and obtain only scoped kubeconfig.
2. Create/apply static script/settings ConfigMaps.
3. Create Job, capture generated name, wait ten minutes.
4. Save Pod log as k6-summary-job.json; on failure save Job describe/Pod log and fail.
5. Require data.metrics.rerank_measure_failure.values.rate less than 0.01 with jq before next VU.
6. Save creation/completion UTC, candidate/VU, fixture/image/SHA, and Job name as metadata JSON.

A different snapshot-reader workflow job queries Prometheus after each completed Job with a 30-second padded start/end and 30-second step. It captures phase p50/p95, outcome rate, max in-flight, CPU seconds, RSS, and CFS throttling. The exact Prometheus Service proxy path is checked-in and matches the Infra Role resourceName; it is not a secret or input. Require successful response status and upload k6/Prometheus/metadata raw JSON with upload-artifact v4.

- [ ] **Step 5: Verify and commit.**

Run:

~~~bash
uv run python -m pytest tests/test_rerank_loadtest_fixture.py -v
git diff --check
if command -v actionlint >/dev/null; then actionlint .github/workflows/rerank-loadtest.yml; fi
~~~

Expected: PASS.

~~~bash
git add deploy/loadtest/rerank-k6-job.yaml .github/workflows/rerank-loadtest.yml tests/test_rerank_loadtest_fixture.py
git commit -m "feat: 리랭킹 GKE 부하테스트 워크플로 추가"
~~~

## Task 6: Document fixture, materialize, and report procedure

**Files:**

- Create: docs/runbooks/rerank-loadtest.md
- Modify: docs/README.md:58-67
- Modify: tests/test_rerank_loadtest_fixture.py
- Create after raw evidence: docs/reports/YYYY-MM-DD-rerank-serving-baseline.html
- Create after one measured change: docs/reports/YYYY-MM-DD-rerank-serving-optimization.html

- [ ] **Step 1: Write failing runbook test.**

~~~python
def test_runbook_requires_materialize_and_raw_artifacts() -> None:
    text = Path("docs/runbooks/rerank-loadtest.md").read_text()
    assert "feast_online_store_materialize" in text
    assert "job_summary.status=succeeded" in text
    assert "rerank-v1" in text
    assert "k6-summary-" in text
    assert "prometheus-range-" in text
    assert "CPU-seconds/request" in text
~~~

- [ ] **Step 2: Verify failure.**

Run:

~~~bash
uv run python -m pytest tests/test_rerank_loadtest_fixture.py::test_runbook_requires_materialize_and_raw_artifacts -v
~~~

Expected: FAIL because runbook is absent.

- [ ] **Step 3: Write exact procedure and report schema.**

Operator executes provisioner dry-run then apply, records version/timestamp/1-1-200-5 counts, manually triggers materialize, and waits for job_summary succeeded. Operator verifies serving readiness and copies deployed image digest/Git SHA into workflow inputs because the loadtest identity cannot read Deployment. Run baseline 24 then 200; refresh fixture/materialize again before optimized and repeat same profiles.

Report one row per candidate/VU; never average 24 and 200. Record custom measurement 중앙값(`med`, p50)/p95/p99, RPS = custom request count / 300, request/status/error count, phase p95, CPU, RSS, throttling, in-flight max, and CPU-seconds/request = CPU seconds rate / RPS. Caption every row with Job, UTC range, fixture/model/image/SHA/resources, artifact URL/hash, and Prometheus query/time range. A missing raw query is N/A with query name, never an improvement.

- [ ] **Step 4: Verify and commit.**

Run:

~~~bash
uv run python -m pytest tests/test_rerank_loadtest_fixture.py -v
git diff --check
~~~

Expected: PASS.

~~~bash
git add docs/runbooks/rerank-loadtest.md docs/README.md tests/test_rerank_loadtest_fixture.py
git commit -m "docs: 리랭킹 부하측정 운영 절차 추가"
~~~

## Task 7: Separate Autoresearch-infra isolation PR

**Files:**

- Create: terraform/admin/autoresearch-k8s/loadtest.tf
- Modify: terraform/admin/autoresearch-k8s/variables.tf
- Modify: terraform/envs/dev/github_actions.tf
- Modify: terraform/envs/dev/variables.tf
- Modify: deploy/monitoring/dashboards/autoresearch-serving.json
- Modify: docs/TEAM_OPERATIONS_RUNBOOK.md
- Create: docs/superpowers/plans/2026-08-01-rerank-loadtest-isolation.md

- [ ] **Step 1: Create separate issue, branch, and plan.**

Read Infra CONTRIBUTING and workflow/security docs. Use its Feature Issue Form with title [FEAT] 리랭킹 GKE 부하테스트 격리, then its Create a branch. Completion condition: loadtest Pod egress is only DNS plus serving TCP 8000, and the two OIDC identities are distinct. Infra CLAUDE requires explicit user confirmation before remote issue/PR/push.

- [ ] **Step 2: Implement namespace and NetworkPolicy.**

Create loadtest and KSA rerank-loadtest with token automount false. Add namespace-wide deny ingress and deny egress. Egress permits only service-CIDR TCP/UDP 53 plus kube-system post-DNAT DNS, and service-CIDR TCP 8000 plus autoresearch namespace/pod label app.kubernetes.io/name=autoresearch-serving post-DNAT TCP 8000. It may not allow public CIDR, metadata IP, Redis, Cloud SQL, BigQuery, Secret Manager, or other service port.

- [ ] **Step 3: Implement RBAC/WIF separation.**

Runner Role: ConfigMaps get/create/update/patch; Jobs get/list/watch/create; Pods get/list/watch; Pods/log get only in loadtest. No delete, exec, portforward, Secret, Deployment, Service write.

Snapshot reader: container.clusterViewer and monitoring services/proxy get on exactly one resolved Prometheus Service. No pod/job/configmap/secret/write. Bind each GSA only to SKYAHO/Autoresearch/.github/workflows/rerank-loadtest.yml@refs/heads/main.

- [ ] **Step 4: Add dashboard and verify Terraform.**

Dashboard adds phase p50/p95, outcome rate, in-flight, CPU/RSS/throttling. It uses sum by le/phase over rerank_phase_duration_seconds_bucket and sum by outcome over rerank_outcomes_total only.

Run:

~~~bash
terraform -chdir=terraform/envs/dev fmt -check -recursive
terraform -chdir=terraform/envs/dev init -backend=false
terraform -chdir=terraform/envs/dev validate
terraform -chdir=terraform/admin/autoresearch-k8s init -backend=false
terraform -chdir=terraform/admin/autoresearch-k8s validate
git diff --check
~~~

Expected: PASS. Apply needs explicit user approval. Infra PR records egress/RBAC, ten-minute Job deadline and one-day TTL cost, and rollback by RoleBinding/WIF removal.

## Task 8: Run baseline, make one evidenced change, rerun

**Files:**

- Create: docs/reports/YYYY-MM-DD-rerank-serving-baseline.html
- Create: docs/reports/YYYY-MM-DD-rerank-serving-optimization.html
- Modify: only source/test files selected by measured dominant phase

- [ ] **Step 1: Baseline first.**

After Infra deployment and materialization success, run all eight profiles. A gate stop is a measured capacity boundary; do not estimate higher VU throughput.

- [ ] **Step 2: Verify evidence.**

Each completed condition has k6 summary, all Prometheus responses, and metadata in one artifact. Hash raw files with sha256sum. Use custom measurement duration/count only, never warmup-inclusive built-in HTTP duration.

- [ ] **Step 3: Select exactly one dominant phase.**

| Dominant evidence | One change | Invariants |
| --- | --- | --- |
| Feast first/second read | bounded TTL cache or concurrent read coalescing | freshness, cold-start, entity key, 21 features |
| assembly | one profiled duplicate conversion removal | category dedup, typed default, order |
| model predict | one DataFrame/model execution change | score, calibration, model ID, order |
| response/in-flight/throttling | one lifecycle/serialization change | public API and single-worker metrics |

Do not select multi-worker, replica/resources, LoadBalancer, or several changes.

- [ ] **Step 4: Fail-first change and same-condition remeasurement.**

Add a regression test for selected invariant, confirm failure, make minimal change, run focused then full serving test, commit separately. Refresh fixture/materialize and repeat the same eight profiles. Pair only equal candidate/VU/fixture/model/resource/window rows.

- [ ] **Step 5: Report observed values only.**

Report p95, RPS, error rate, CPU-seconds/request only where a baseline/optimized pair and raw artifact hash exist. Only then may a resume statement cite the exact candidate and VU condition.

## Final Verification

- [ ] uv run python -m pytest tests/test_serving_online_features.py tests/test_serving_api.py tests/test_rerank_loadtest_fixture.py -v
- [ ] uv run python -m pytest -v
- [ ] uv run --no-sync ruff check agent_orchestration autoresearch tests tools
- [ ] Run the Feast-isolated test list declared in .github/workflows/ci.yml.
- [ ] docker build -f deploy/serving/Dockerfile -t autoresearch-serving:issue-455 .
- [ ] git diff --check and actionlint .github/workflows/rerank-loadtest.yml when actionlint is installed.
- [ ] Infra Terraform/policy review passes before live workflow dispatch.
- [ ] Do not publish p95/RPS/cost improvement before baseline and optimized raw reports exist.

## Commit Order

1. docs: 리랭킹 성능 측정 구현계획 추가
2. feat: 리랭킹 피처 조립 단계 시간 추가
3. feat: 리랭킹 단계별 서빙 메트릭 추가
4. feat: 리랭킹 부하테스트 fixture 추가
5. test: 리랭킹 k6 요청 계약 추가
6. feat: 리랭킹 GKE 부하테스트 워크플로 추가
7. docs: 리랭킹 부하측정 운영 절차 추가
8. perf: 관측된 리랭킹 병목 개선

When implementation and measured reports finish, move this plan to docs/archive/plans. Keep the runbook and reports in their current locations.
