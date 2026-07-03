# Agent Project Reference

> Last Updated: 2026-07-03

Quick navigation to project ownership, folder structure, and team boundaries.
This doc answers "where does X live?" and "who owns Y?" questions.

## When To Use This Doc

- You need to understand the project layout and folder responsibilities.
- You're adding new code and want to know where it belongs.
- You need to understand team domain boundaries and ownership.

## Project Layout

```
src/
├── config/              # Shared configuration utilities
├── features/            # Feast Feature Store definitions & feature engineering
│   ├── feature_builder.py
│   └── ...
├── models/              # Model definitions and training logic
│   ├── lightgbm_model.py
│   └── ...
├── pipeline/            # Training pipeline, evaluation, and orchestration
│   ├── config.yaml      # Pipeline configuration
│   ├── train.py         # Training script
│   ├── evaluate.py      # Model evaluation
│   ├── build_training_dataset.py
│   └── ...
├── utils/               # Utility functions (logging, data handling, etc.)
│   └── ...
└── cli/                 # CLI interface (future)

artifacts/
├── models/              # Trained models and checkpoints (git-ignored)
└── ...

tests/
├── unit/                # Unit tests for modules
├── integration/         # Integration tests
└── ...

docs/
├── specs/YYYY-MM-DD-<slug>.md   # Requirements and design decisions
└── plans/YYYY-MM-DD-<slug>.md   # Implementation plans
```

## Team Ownership & Domains

| Domain | Team Members | Responsibilities | Key Paths |
|--------|--------------|------------------|-----------|
| **Model Training** | waieiches, hyochangsung | Model architecture, training pipelines, evaluation metrics | `src/models/`, `src/pipeline/` |
| **Feast Features** | waieiches, hyochangsung | Feature definitions (ODFV), feature engineering, feature store integration | `src/features/` |
| **Airflow Orchestration** | bbungjun | DAG definitions, job scheduling, data pipeline orchestration | `src/pipeline/airflow/` (future) |
| **GCP Infrastructure** | hyeongyu-data | Cloud deployment, infrastructure-as-code, secrets management | `.github/workflows/`, `infrastructure/` (future) |

## Ownership Boundaries

### `src/config/`
- **Owner:** Shared (all domains)
- **Responsibility:** Centralized configuration loading, environment variable handling
- **Pattern:** Config utilities go here; don't create domain-specific config files

### `src/models/`
- **Owner:** 대장, 효창 (Model Training)
- **Responsibility:** Model classes, training logic, hyperparameter definitions
- **Pattern:** One model = one file (e.g., `lightgbm_model.py`). Keep model-specific logic internal.

### `src/features/`
- **Owner:** 대장, 효창 (Feast Features)
- **Responsibility:** Feast FeatureView definitions (ODFV-first pattern), feature engineering transforms
- **Key Decision:** ODFV (On-Demand Feature View) required; never use generic FeatureView for transformations
- **Pattern:** Feature definitions → `features.py`. Helper transforms → `_transforms.py`. Integration tests in `tests/integration/`.

### `src/pipeline/`
- **Owner:** 대장, 효창 (Model Training), 영준 (Airflow)
- **Responsibility:** Training orchestration, evaluation, dataset building, config schema
- **Pattern:** One orchestration script = one file. CLI interface wraps these.
- **Config:** `config.yaml` is the single source of truth for pipeline parameters.

### `src/utils/`
- **Owner:** Shared (all domains)
- **Responsibility:** Reusable utilities, helpers, logging, validation
- **Pattern:** If used by 2+ domains, it belongs here. Domain-specific helpers stay in-domain.

## Technical Stack

- **Language:** Python 3.9+
- **Dependencies:** `uv` (package manager), PyTorch, LightGBM, Feast, Airflow (future)
- **Data Storage:** DuckDB (local), BigQuery (production)
- **Model Artifacts:** `artifacts/models/` (local git-ignored directory)
- **Configuration:** YAML (`src/pipeline/config.yaml`) + environment variables
- **Testing:** pytest (unit + integration tests)
- **Linting/Typing:** ruff, basedpyright

## Key Extension Rules

### Adding New Features

1. **Determine domain ownership:** Is this Model Training, Feast, Airflow, or GCP?
2. **Place code correctly:** Follow folder structure. Avoid cross-domain entanglement.
3. **Update config if needed:** If it affects pipeline behavior, add to `config.yaml`.
4. **Write tests:** Unit tests in `tests/unit/`, integration tests in `tests/integration/`.
5. **Document design decisions:** If the change affects architecture, add a note to the relevant `.claude/docs/` guide or create a spec document.

### When Domains Overlap

- **Model + Features:** Feature engineering lives in `src/features/`. Model training consumes features in `src/models/`.
- **Model + Pipeline:** Pipeline orchestrates training; model implementation stays in `src/models/`.
- **Features + Airflow:** Airflow DAG retrieves features from Feast; feature definitions stay in `src/features/`.

## Verification Checklist

- [ ] Code is in the correct folder per team domain.
- [ ] Configuration changes go to `config.yaml` (not hardcoded).
- [ ] Tests are written for new functionality.
- [ ] No cross-domain entanglement (e.g., feature transforms in `models/`).
- [ ] Docs updated if behavior or configuration changed.
