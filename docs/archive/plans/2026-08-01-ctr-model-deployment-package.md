# CTR Model Deployment Package Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** #302의 서빙 pickle 경로를 제거하고, 단일 MLflow run의 ONNX·JSON·calibration을 `ctr-model-package-v1` manifest로 fail-closed 검증합니다.

**Architecture:** `src/tracking/model_package.py`가 manifest 스키마와 canonical hash를 소유합니다. 학습은 프로세스 전용 staging에서 ONNX 패키지와 manifest를 완성·자체 검증한 뒤 run에 기록하고 마지막에만 모델 버전을 만듭니다. 서빙은 run 아티팩트를 자신이 소유한 임시 디렉터리로 복사해 검증한 동일 ONNX 파일로 세션을 만들며, 세션을 감싼 모델이 임시 디렉터리 소유권도 보유합니다.

**Tech Stack:** Python 3.11/3.12, pydantic v2, MLflow ONNX, onnxruntime, pytest, Ruff

## Global Constraints

- 설계 정본은 `docs/specs/2026-08-01-ctr-model-deployment-package.md`입니다.
- `sampling_rate`는 유한수 및 `0 < value <= 1`을 먼저 검증한 뒤 `value < 1.0`과 `else`(`== 1.0`)로 정확히 분기합니다. tolerance 비교를 사용하지 않습니다.
- Windows에서는 재귀 진입 또는 파일 열기 전에 각 경로의 symlink, junction, 기타 reparse-point 여부를 검사하고 거부합니다.
- ONNX Runtime 세션이 살아 있는 동안 프로세스 전용 `TemporaryDirectory`도 살아 있어야 합니다. `OnnxProbabilityModel`이 세션과 workspace owner를 함께 보유하며 모델 수명 종료 때 정리합니다.
- 새·변경 런타임 모듈의 module responsibility docstring과 모든 함수 반환 타입을 유지합니다.
- Airflow 및 인프라 저장소는 변경하지 않습니다.

---

### Task 1: Manifest 스키마와 canonical hash

**Files:**
- Create: `src/tracking/model_package.py`
- Create: `tests/test_model_package.py`

**Interfaces:**
- Produces: `ModelPackageManifest`, `build_model_package_manifest(...) -> ModelPackageManifest`, `save_manifest(...) -> None`, `load_manifest(...) -> ModelPackageManifest`, `sha256_file(...) -> str`, `sha256_directory(...) -> str`, `verify_model_package(...) -> None`

- [ ] **Step 1: 실패 테스트 작성** — extra field, lowercase 64-hex, 고정 상대 경로, sampling-rate/calibration 정확 분기, NaN, calibration 값 불일치, 파일 hash, 길이-prefix directory hash, 파일명·추가파일 변화, symlink/reparse-point 선거부를 테스트합니다.
- [ ] **Step 2: RED 확인**

  ```powershell
  uv run python -m pytest tests/test_model_package.py -v
  ```

  Expected: `src.tracking.model_package`가 없어 collection 실패.

- [ ] **Step 3: 최소 구현** — pydantic 모델은 모두 `ConfigDict(extra="forbid")`; SHA 필드는 `^[0-9a-f]{64}$`; `sampling_rate`는 `math.isfinite`와 범위 검증 후 `< 1.0`/`else`로 calibration을 검증합니다. directory hash는 각 항목에 `struct.pack(">Q", len(...))` 길이 prefix를 사용합니다. Windows reparse point는 `Path.stat(follow_symlinks=False).st_file_attributes & stat.FILE_ATTRIBUTE_REPARSE_POINT`를 재귀 진입·open 전에 검사합니다.
- [ ] **Step 4: GREEN 확인**

  ```powershell
  uv run python -m pytest tests/test_model_package.py -v
  uv run --no-sync ruff check src/tracking/model_package.py tests/test_model_package.py
  ```

- [ ] **Step 5: 커밋**

  ```powershell
  git add src/tracking/model_package.py tests/test_model_package.py
  git commit -m "feat: 모델 배포 manifest 계약 추가"
  ```

### Task 2: ONNX 입출력 계약과 workspace 수명

**Files:**
- Modify: `src/serving/onnx_model.py`
- Modify: `tests/test_serving_onnx.py`

**Interfaces:**
- Produces: `validate_onnx_session_contract(session, feature_count: int) -> None`
- Changes: `OnnxProbabilityModel(session, feature_columns, workspace_owner=None)`가 owner를 인스턴스 수명 동안 보유

- [ ] **Step 1: 실패 테스트 작성** — 입력 개수/name/dtype/shape, 출력 dtype/shape 오류가 각각 실패하고, 모델이 살아 있는 동안 workspace 파일이 존재하며 모델 삭제·GC 후 정리되는지 검증합니다.
- [ ] **Step 2: RED 확인**

  ```powershell
  uv run python -m pytest tests/test_serving_onnx.py -v
  ```

- [ ] **Step 3: 최소 구현** — session metadata를 검사하고 `OnnxProbabilityModel`이 `_workspace_owner`를 강한 참조로 보유하게 합니다. `predict_proba`에서도 실제 출력 shape `(n, 2)`를 확인합니다.
- [ ] **Step 4: GREEN 확인 및 커밋**

  ```powershell
  uv run python -m pytest tests/test_serving_onnx.py -v
  git add src/serving/onnx_model.py tests/test_serving_onnx.py
  git commit -m "feat: ONNX 기동 계약과 workspace 수명 검증"
  ```

### Task 3: 학습 패키지 fail-closed 생성

**Files:**
- Modify: `src/tracking/logger.py`
- Modify: `src/pipeline/train.py`
- Modify: `tests/test_pipeline_train.py`

**Interfaces:**
- Consumes: Task 1 manifest API
- Produces: `save_onnx_model(onnx_model, path: Path) -> None`; pending registration URI `runs:/<run_id>/model_onnx`

- [ ] **Step 1: 실패 테스트 작성** — ONNX package·manifest가 모두 로깅되고 자체 검증 뒤 pending registration이 만들어지는지, ONNX/manifest 실패 시 등록 후보가 없고 예외가 전파되는지, sampling rate 정확 분기와 calibration manifest 항목을 검증합니다.
- [ ] **Step 2: RED 확인**

  ```powershell
  uv run python -m pytest tests/test_pipeline_train.py -k "onnx or manifest or calibration" -v
  ```

- [ ] **Step 3: 최소 구현** — `TemporaryDirectory` staging에 `mlflow.onnx.save_model`, JSON, calibration, manifest를 순서대로 만들고 검증합니다. `sampling_rate` 범위 검증 뒤 `< 1.0`이면 calibration 객체, `else`는 정확히 `1.0`이고 `null`로 만듭니다. best-effort `try/except`를 제거하고, 모든 log 성공 뒤 `PendingRegistration`을 생성합니다.
- [ ] **Step 4: GREEN 확인 및 커밋**

  ```powershell
  uv run python -m pytest tests/test_pipeline_train.py -k "onnx or manifest or calibration" -v
  git add src/tracking/logger.py src/pipeline/train.py tests/test_pipeline_train.py
  git commit -m "feat: 학습 배포 패키지를 fail-closed로 생성"
  ```

### Task 4: 서빙 joblib 제거와 manifest 검증

**Files:**
- Modify: `src/serving/model_loader.py`
- Modify: `tests/test_serving_api.py`
- Modify: `tests/test_serving_onnx.py`
- Modify: `tests/test_serving_model_registry.py`
- Modify: `.env.example`

**Interfaces:**
- Consumes: Task 1 검증 API, Task 2 ONNX 계약과 workspace owner
- Changes: `LocalModelSettings`는 ONNX·JSON·manifest 경로만 받음; MLflow/Registry는 `manifest/manifest.json`을 필수 다운로드

- [ ] **Step 1: 실패 테스트 작성** — ONNX 부재·손상·hash 불일치·manifest 오류가 `ModelArtifactError`; joblib 다운로드가 호출되지 않음; registry version run ID만 모든 다운로드에 사용; 프로세스 전용 복사본을 검증하고 session 수명 동안 유지함을 검증합니다.
- [ ] **Step 2: RED 확인**

  ```powershell
  uv run python -m pytest tests/test_serving_api.py tests/test_serving_onnx.py tests/test_serving_model_registry.py -k "model or manifest or onnx or registry" -v
  ```

- [ ] **Step 3: 최소 구현** — `import joblib`, `MLFLOW_MODEL_ARTIFACT_PATH`, `_try_load_onnx_session_from_run`, joblib branch, `RERANK_MODEL_PATH`를 제거합니다. MLflow 다운로드 결과를 새 `TemporaryDirectory`로 복사하고 검증한 동일 `model.onnx`으로 session을 생성한 뒤 owner를 `OnnxProbabilityModel`에 넘깁니다.
- [ ] **Step 4: GREEN 확인 및 커밋**

  ```powershell
  uv run python -m pytest tests/test_serving_api.py tests/test_serving_onnx.py tests/test_serving_model_registry.py -v
  rg -n "joblib\.load|MLFLOW_MODEL_ARTIFACT_PATH|RERANK_MODEL_PATH" src/serving .env.example
  git add src/serving/model_loader.py src/serving/onnx_model.py tests/test_serving_api.py tests/test_serving_onnx.py tests/test_serving_model_registry.py .env.example
  git commit -m "feat: 서빙 모델 패키지를 ONNX 전용으로 검증"
  ```

### Task 5: 문서 정합성과 완료 검증

**Files:**
- Modify: `docs/guides/ctr-model-specification.md`
- Modify: `docs/specs/2026-07-16-reranking-serving-api.md`
- Modify: `docs/README.md`
- Move after completion: `docs/specs/2026-08-01-ctr-model-deployment-package.md` → `docs/archive/specs/`
- Move after completion: `docs/plans/2026-08-01-ctr-model-deployment-package.md` → `docs/archive/plans/`

- [ ] **Step 1: 문서의 joblib 폴백·manifest 미완료 서술을 현행 계약으로 갱신하고 인덱스를 맞춥니다.**
- [ ] **Step 2: 좁은 검증 실행**

  ```powershell
  uv run python -m pytest tests/test_model_package.py tests/test_model_utils.py tests/test_pipeline_train.py tests/test_serving_api.py tests/test_serving_onnx.py tests/test_serving_model_registry.py -v
  uv run --no-sync ruff check src tests
  git diff --check
  ```

- [ ] **Step 3: 전체 검증 실행**

  ```powershell
  uv run python -m pytest -v
  uv run --no-sync ruff check agent_orchestration autoresearch tests tools
  docker build -f deploy/serving/Dockerfile -t autoresearch-serving:issue-302 .
  ```

- [ ] **Step 4: 완료 문서 아카이브 및 커밋**

  ```powershell
  git add docs
  git commit -m "docs: CTR 배포 패키지 완료 상태 반영"
  ```

### Task 6: PR과 PR Report

- [ ] **Step 1:** 변경 범위·커밋·검증 결과를 재확인하고 브랜치를 push합니다.
- [ ] **Step 2:** `feature` label, 본인 assignee, `Closes #302`를 포함한 Ready PR을 생성합니다.
- [ ] **Step 3:** PR checks와 자동 리뷰 상태를 확인합니다.
- [ ] **Step 4:** PR conversation에 `/claude-report`를 작성해 PR Report를 생성하고 workflow 결과·sticky comment URL을 확인합니다.
