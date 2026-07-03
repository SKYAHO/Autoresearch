# Architecture Overview

> Last Updated: 2026-07-03

High-level overview of the four domain architecture, key design decisions, and
how they interact.

## When To Use This Doc

- You need to understand how the four domains interact.
- You're making a cross-domain change and want to understand constraints.
- You need to understand why specific architectural choices were made (e.g., ODFV vs FeatureView).
- You're onboarding to the project and want a bird's-eye view.

## Four Domains

```
┌─────────────────────────────────────────────────────────┐
│                  Autoresearch Project                   │
├─────────────────────────────────────────────────────────┤
│                                                         │
│  Model Training      Feast Features       Airflow       │
│  (waieiches,         (waieiches,          (bbungjun)   │
│   hyochangsung)      hyochangsung)                      │
│                                                         │
│  ↓                   ↓                   ↓               │
│  LightGBM          ODFV FeatureViews  DAG Scheduler    │
│  CTR Prediction    Feature Transform   Orchestration   │
│                                                         │
│  ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← ← → → → → → →   │
│         Consumes Features    Calls Training/Eval       │
│                                                         │
└─────────────────────────────────────────────────────────┘
                            ↑
                 GCP Infrastructure
                    (hyeongyu-data)
        [Cloud Deployment, Secrets, Auth]
```

## Domain 1: Model Training (waieiches, hyochangsung)

**Responsibility:** CTR (Click-Through Rate) model definition, training orchestration, evaluation metrics.

**Key Files:**
- `src/models/lightgbm_model.py` — LightGBM model class
- `src/pipeline/train.py` — Training script
- `src/pipeline/evaluate.py` — Evaluation and metric calculation
- `src/pipeline/build_training_dataset.py` — Dataset preparation from Event Log
- `src/pipeline/config.yaml` — Model hyperparameters and paths
- `docs/CTR_Model_Specification.md` — Full CTR modeling specification

**Inputs:**
- Event Log (from Agent Simulator, see AGENT_SIMULATOR_SPEC.md)
- Feast features (retrieved at training time)

**Outputs:**
- Trained model checkpoint → `artifacts/models/`
- Evaluation metrics → logs and artifacts

### CTR Model Overview (see docs/CTR_Model_Specification.md for full details)

**Modeling Task:**
- **Target:** Predict click probability when a user_id sees a video_id
- **Input:** user_id (retrieved from API), features assembled from Feature Store
- **Output:** Click probability per video (not a recommendation list)
- **Post-processing:** Rank by probability, extract Top-N, optionally mix exploration items

**Feature Engineering:**

| Feature Category | Examples | Storage |
|------------------|----------|---------|
| Video Features | category_id, duration_sec, view_count, like_ratio, comment_ratio, days_since_upload | Offline (Batch) |
| User Features | age_group, occupation, historical_category_affinity, recent_click_count_7d, recent_watch_time_7d, recent_like_count_7d | Online (streaming) |
| Intermediate Artifacts | preferred_topics, video_topic, user_embedding, video_embedding | Computed on-demand |

**Key Rules (see CTR_Model_Specification.md):**
- Scalar features only; vectors/lists used only for Similarity computation, not direct input
- User features derived only from events **before label timestamp** (no leakage)
- Interaction features computed identically in training and serving (no training-serving skew)
- Similarity features abstracted by **score**, not implementation (baseline defined, but BM25/Cosine/Cross-Encoder variations allowed)
- Cold-start policy: missing historical_category_affinity → "unknown" (not imputed)

**Design Decisions:**
- Use Feast at training time to fetch latest features dynamically
- Evaluation includes offline metrics and feature analysis
- Single source of truth for data generation: AGENT_SIMULATOR_SPEC.md defines Event Log; CTR_Model_Specification.md defines feature/label transformation

## Domain 2: Feast Features (waieiches, hyochangsung)

**Responsibility:** Feature definitions, feature store setup, feature engineering transforms.

**Key Files:**
- `src/features/feature_builder.py` — Feature classes and transforms
- `src/features/features.py` — Main FeatureView definitions

**Inputs:**
- Event log data (from data sources)
- Historical feature request timestamps
- Raw data (YouTube API, Persona, Event Log)

**Outputs:**
- Retrieved features (used by training and inference)
- Feature metadata (columns, types, freshness)

### Key Design Decisions for Feast

**1. ODFV (On-Demand Feature View) is mandatory**

- **What:** ODFV allows real-time feature transformations without pre-computing
- **Why:** We need flexibility to apply transforms (normalization, bucketing) on-the-fly without storing every possible variation
- **When to use:** Transforms that apply to real-time requests (e.g., "last 7 days of activity")
- **Anti-pattern:** Don't use generic `FeatureView` for transforms; that requires pre-materialization of every variant

**Example (correct):**
```python
@on_demand_feature_view(...)
def user_activity_odfv(inputs):
    # Compute on-demand, not pre-stored
    return df.with_columns([
        pl.col("event_count").rolling_mean(window_size=7).alias("activity_7d_ma")
    ])
```

**Example (incorrect):**
```python
# Don't do this — requires pre-computing all normalization variants
@batch_feature_view(...)
def user_activity_batch(inputs):
    ...
```

**2. TTL (Time-To-Live) ≠ Windowed Aggregation**

- **TTL:** How fresh the feature data must be (e.g., "features must be refreshed hourly")
- **Windowed Agg:** Window size for computation (e.g., "sum of last 7 days")
- **Common mistake:** Confusing `ttl=3600` (1 hour) with `window_size=7*24*3600` (7 days)
- **Rule:** Set both independently based on business requirements, not as the same value

**Example:**
```yaml
# Correct: features refresh every hour, but compute over 7 days
ttl: 3600  # 1 hour freshness requirement
window_size: 604800  # 7 days of data (7*24*3600)
```

**3. Cold-Start Fallback: Use "unknown" or Null**

- **What:** When a user/entity has no historical data, what do we return?
- **Decision:** Return explicit null or "unknown" identifier, NOT a default value
- **Why:** Explicit missing values prevent silent bugs; the model can learn to handle them
- **Anti-pattern:** Don't impute with zeros or means during serving; let the model see the sparsity it trained on

**Example:**
```python
def get_feature_value(user_id, feature_name):
    value = fetch_from_feast(user_id, feature_name)
    if value is None:
        return None  # or "UNKNOWN_USER" as a categorical
    return value
```

**4. Training-Serving Consistency**

- **Rule:** Interaction features (e.g., category similarity) must be computed identically in training and serving
- **Why:** Training-Serving Skew is a major source of production model degradation
- **Enforcement:** Code reviews check that training dataset feature logic and Feast transform logic are identical

## Domain 3: Airflow Orchestration (bbungjun)

**Responsibility:** DAG definitions, job scheduling, pipeline orchestration.

**Key Files:**
- `dags/` — DAG definitions
- `src/pipeline/config.yaml` — Shared config (read by DAGs)
- `airflow_settings.yaml` — Airflow environment configuration

**Inputs:**
- Event logs (from data sources)
- Scheduling triggers (daily, hourly, etc.)

**Outputs:**
- Trigger training jobs
- Orchestrate feature refreshes
- Monitor pipeline health

**Example DAGs:**
- `youtube_backfill_kr.py` — Backfill historical data
- `youtube_trending_kr_daily.py` — Daily data ingestion and model retraining

**Design Constraints:**
- DAGs consume `config.yaml` for parameter management (don't hardcode)
- Training and evaluation scripts are wrapped by DAG operators (BashOperator)
- Feature refreshes are separate DAGs that run before training
- All DAG code references Domain 1/2 modules; no duplicate feature/model logic

## Domain 4: GCP Infrastructure (hyeongyu-data)

**Responsibility:** Cloud deployment, environment setup, secrets management.

**Key Areas:**
- GitHub Actions workflows for CI/CD (`.github/workflows/ci.yml`)
- GCP service accounts and IAM
- Secret management (API keys, credentials)
- Data pipeline infrastructure (BigQuery, Cloud Composer)

**Integration Points:**
- All domains: Credentials stored in GitHub Secrets / GCP Secret Manager (`.env.example` for local)
- Feature domain: BigQuery backend for Feast (production)
- Model domain: GCP Artifact Registry for model storage
- Airflow domain: Cloud Composer orchestration

## Cross-Domain Interactions

### Model Training ← Feast Features
- **Flow:** Training script calls Feast client to retrieve features
- **Contract:** Features must be retrieved at consistent timestamp; no training-serving skew
- **Example:** `feast.get_online_features(entity_rows=[...], features=[...])`

### Airflow → Model Training + Feast
- **Flow:** Airflow DAG calls training script via BashOperator
- **Contract:** Training script exits 0 on success, non-zero on failure
- **Example:** `BashOperator(bash_command="python src/pipeline/train.py")`

### All Domains ← GCP Infrastructure
- **Credentials:** Every script reads from environment vars or GitHub Secrets
- **Data:** BigQuery for raw events, DuckDB/SQLite for local development
- **Artifacts:** Model checkpoints stored in `artifacts/` (local) or GCP Artifact Registry (production)

## Key Architecture Rules

1. **No tight coupling between domains.** If Model Training needs Airflow details, that's a coupling issue.
2. **Config is single-source-of-truth.** All runtime behavior defined in `config.yaml`, not code.
3. **Feast is the feature source.** No direct SQL queries for features in training scripts; always go through Feast.
4. **Secrets are environment variables.** No hardcoding credentials, API keys, or paths.
5. **Each domain has a clear owner.** Ownership questions → check `agent-project-reference.md`.
6. **Single source of truth for data specification.** Event Log spec → AGENT_SIMULATOR_SPEC.md. Feature/Label definition → CTR_Model_Specification.md.

## Verification Checklist

- [ ] New code belongs to the correct domain (check `agent-project-reference.md`)
- [ ] No cross-domain hardcoding of credentials or paths
- [ ] Feast uses ODFV for transforms (not batch FeatureView)
- [ ] TTL and windowed aggregation are set independently
- [ ] Cold-start handling is explicit (null or "unknown", not imputed)
- [ ] Config changes go to `config.yaml`, not code
- [ ] Cross-domain tests validate interaction points
- [ ] Training and serving feature logic is identical (no training-serving skew)
- [ ] Specs (CTR_Model_Specification.md, AGENT_SIMULATOR_SPEC.md) are the source of truth
