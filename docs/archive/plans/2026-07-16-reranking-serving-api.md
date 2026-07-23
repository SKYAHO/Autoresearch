# Reranking Serving API 구현 계획

## 초기 구현 계획 (완료)

1. Pydantic 요청·응답 계약, 모델 artifact loader, CTR scoring 서비스를 추가한다.
2. FastAPI `/healthcheck`, `/rerank`, `/metrics`와 모델 준비 상태를 연결한다.
3. serving image 및 환경 변수 사용법을 추가한다.
4. 로컬·MLflow loader, 정렬, healthcheck, metrics를 테스트하고 Docker build를 검증한다.

---

# 코드 리뷰 후속 개선 구현 계획 (2026-07-16) (완료)

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [x]`) syntax for tracking.

**Goal:** feat/160 브랜치 코드 리뷰에서 발견된 6개 결함(CI 의존성 파손, 학습·서빙 범주형 불일치, 예외 매핑 누락, 히스토그램 버킷 오용, 이중 루프, MLflow 경로 하드코딩)을 수정한다.

**Architecture:** 학습 파이프라인이 범주형 컬럼의 **카테고리 값·순서**를 새 아티팩트(`categorical_columns.pkl`, `dict[컬럼명, 카테고리 리스트]`)로 저장하고, 서빙이 이를 로드해 `pd.Categorical(values, categories=학습카테고리)`로 캐스팅한다. LightGBM은 predict 시 category **코드**를 사용하므로 카테고리 값·순서가 학습과 동일해야 예측이 올바르다. 의존성은 `dev` 그룹이 `serving` 그룹을 include하여 CI 테스트 표면을 복원한다.

**Tech Stack:** Python 3.12, FastAPI, LightGBM, pandas, prometheus-client, uv, pytest

## Global Constraints

- Python 3.12 (`.python-version`), CI는 3.11/3.12 매트릭스
- 모든 Python 함수에 타입 힌트(반환 타입 포함) 유지
- 의존성 변경: `pyproject.toml` 수정 → `uv lock` → 산출물 갱신 순서
- 커밋 메시지: `<type>: 한국어 설명` 형식, 말미에 `Co-Authored-By: Claude Fable 5 <noreply@anthropic.com>`
- 이 브랜치(feat/160, 이슈 #160)에서 작업 — 새 이슈/브랜치 불필요 (리뷰 후속 수정)
- `src/pipeline/evaluate.py`의 동일 계열 결함(테스트셋 단독 `astype("category")` → 코드 불일치 가능)은 **이 브랜치 범위 밖** — 수정하지 말고 별도 이슈로 제안만 한다
- 시크릿·로컬 데이터·생성 파일·`.env` 커밋 금지 (`output/`, `__pycache__/` 주의)

## 아티팩트 계약 (Task 2·3이 공유)

| 아티팩트 | 로컬 경로 (config.yaml) | MLflow 경로 | 형식 |
| --- | --- | --- | --- |
| 모델 | `artifacts/models/lgbm_model.joblib` | `runs:/<run_id>/model/lgbm_model.joblib` | joblib |
| feature 목록 | `artifacts/models/feature_columns.pkl` | `runs:/<run_id>/features/feature_columns.pkl` | pickle `list[str]` |
| **범주형 카테고리 (신규)** | `artifacts/models/categorical_columns.pkl` | `runs:/<run_id>/features/categorical_columns.pkl` | pickle `dict[str, list]` — 키는 컬럼명, 값은 학습 시점 카테고리(순서 보존) |

범주형 아티팩트는 서빙에서 **필수**다. 없는 기존 MLflow run은 재학습이 필요하며, 서빙은 명확한 `ModelArtifactError`로 실패한다 (조용히 틀린 예측보다 명시적 실패 우선).

---

### Task 1: dev 의존성 그룹에 serving 포함 (CI 파손 수정)

**Files:**
- Modify: `pyproject.toml` (dependency-groups `dev`)
- Modify: `uv.lock` (`uv lock`으로 재생성)

**Interfaces:**
- Produces: CI(`uv sync --frozen`, default-groups=dev)에서 `fastapi`, `uvicorn`, `prometheus-client` 설치 가능 — `tests/test_serving_api.py`, `tests/test_proxy_app.py`가 import 성공

**배경:** 이 브랜치가 `fastapi>=0.115,<0.129`를 `dev` 그룹에서 제거하고 새 `serving` 그룹으로만 옮겼다. CI는 dev 그룹만 설치하므로 fastapi를 import하는 테스트 2개가 collection 단계에서 실패한다. `serving` 그룹을 dev에 include하면 중복 선언 없이 복원된다 (`lint`가 이미 같은 패턴 사용). `feast` 그룹과의 충돌은 `[tool.uv] conflicts`에 dev·serving 모두 이미 선언되어 있어 추가 작업이 없다.

- [x] **Step 1: 현재 실패 재현**

```bash
uv sync && uv run python -m pytest tests/test_serving_api.py tests/test_proxy_app.py -v
```

Expected: `ModuleNotFoundError: No module named 'fastapi'` (또는 `prometheus_client`)로 collection 실패. (로컬 `.venv`에 이전 설치가 남아 통과한다면 `uv sync`가 pruning했는지 확인 — `uv pip list | grep fastapi`가 비어야 실패 재현됨.)

- [x] **Step 2: pyproject.toml 수정**

`[dependency-groups]`의 `dev`를 다음으로 변경:

```toml
dev = [
    "pytest>=8.0",
    "datasets>=2.19",
    "httpx>=0.28,<0.29",
    "google-cloud-bigquery>=3.20",
    "python-dotenv>=1.0",
    { include-group = "lint" },
    { include-group = "serving" },
]
```

- [x] **Step 3: lockfile 재생성 및 검증**

```bash
uv lock && uv sync --frozen && uv run --no-sync python -m pytest tests/test_serving_api.py tests/test_proxy_app.py -v
```

Expected: 전체 PASS. (`uv sync --frozen` + `uv run --no-sync`는 CI와 동일한 호출 형태.)

- [x] **Step 4: 전체 테스트 실행**

```bash
uv run --no-sync python -m pytest -v
```

Expected: 전체 PASS.

- [x] **Step 5: Commit**

```bash
git add pyproject.toml uv.lock
git commit -m "fix: dev 그룹에 serving 포함으로 CI 테스트 의존성 복원"
```

---

### Task 2: 학습 파이프라인 — 범주형 카테고리 아티팩트 저장

**Files:**
- Modify: `src/pipeline/config.yaml` (artifacts 섹션)
- Modify: `src/utils/model_utils.py` (save/load 함수 추가)
- Modify: `src/pipeline/train.py:129-137` (Step 4), `src/pipeline/train.py:190-208` (Step 8), `main()` 시그니처
- Modify: `src/cli.py` (`train_model`, `run_pipeline`에 옵션 추가)
- Test: `tests/test_model_utils.py` (신규), `tests/test_pipeline_train.py` (신규)

**Interfaces:**
- Produces: `collect_categorical_categories(X_train: pd.DataFrame, X_val: pd.DataFrame, categorical_columns: list[str]) -> dict[str, list]` (in `src/pipeline/train.py`) — 두 DataFrame을 category dtype으로 **변환(mutate)**하고 union 카테고리 dict 반환
- Produces: `save_categorical_columns(categories_by_column: dict, path: str) -> None`, `load_categorical_columns(path: str) -> dict` (in `src/utils/model_utils.py`)
- Produces: 학습 산출물 `artifacts/models/categorical_columns.pkl` + MLflow `features/categorical_columns.pkl` — Task 3의 서빙 로더가 소비

- [x] **Step 1: 실패하는 테스트 작성 — model_utils 라운드트립**

`tests/test_model_utils.py` 생성:

```python
from __future__ import annotations

from pathlib import Path

from src.utils.model_utils import load_categorical_columns, save_categorical_columns


def test_categorical_columns_roundtrip(tmp_path: Path) -> None:
    categories_by_column = {
        "category_id": [10, 20, 30],
        "age_group": ["10s", "20s", "30s"],
    }
    path = tmp_path / "categorical_columns.pkl"

    save_categorical_columns(categories_by_column, str(path))
    loaded = load_categorical_columns(str(path))

    assert loaded == categories_by_column
```

- [x] **Step 2: 실패 확인**

```bash
uv run python -m pytest tests/test_model_utils.py -v
```

Expected: FAIL — `ImportError: cannot import name 'save_categorical_columns'`

- [x] **Step 3: model_utils 구현**

`src/utils/model_utils.py` 말미에 추가 (기존 `save_feature_columns` 스타일 유지):

```python
def save_categorical_columns(categories_by_column: dict, path: str) -> None:
    """
    범주형 컬럼별 카테고리 목록을 pickle 형식으로 저장.

    서빙이 학습과 동일한 category 코드 매핑을 재현하는 데 사용한다.

    Args:
        categories_by_column: 컬럼명 -> 학습 시점 카테고리 리스트(순서 보존).
        path: 저장 경로.
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        pickle.dump(categories_by_column, f)
    print(f"[저장 완료] categorical_columns: {path}")


def load_categorical_columns(path: str) -> dict:
    """
    pickle 형식의 범주형 카테고리 목록 로드.

    Args:
        path: 로드 경로.

    Returns:
        컬럼명 -> 카테고리 리스트 dict.
    """
    if not os.path.exists(path):
        raise FileNotFoundError(f"Categorical 컬럼 파일을 찾을 수 없습니다: {path}")
    with open(path, "rb") as f:
        categories_by_column = pickle.load(f)
    print(f"[로드 완료] categorical_columns: {path} ({len(categories_by_column)} columns)")
    return categories_by_column
```

- [x] **Step 4: 테스트 통과 확인**

```bash
uv run python -m pytest tests/test_model_utils.py -v
```

Expected: PASS

- [x] **Step 5: 실패하는 테스트 작성 — collect_categorical_categories**

`tests/test_pipeline_train.py` 생성:

```python
from __future__ import annotations

import pandas as pd

from src.pipeline.train import collect_categorical_categories


def test_collect_categorical_categories_unions_train_and_val() -> None:
    X_train = pd.DataFrame({"category_id": [20, 10], "duration_sec": [1.0, 2.0]})
    X_val = pd.DataFrame({"category_id": [30], "duration_sec": [3.0]})

    result = collect_categorical_categories(X_train, X_val, ["category_id"])

    assert result == {"category_id": [10, 20, 30]}
    assert str(X_train["category_id"].dtype) == "category"
    assert list(X_train["category_id"].cat.categories) == [10, 20, 30]
    assert list(X_val["category_id"].cat.categories) == [10, 20, 30]
    # 비범주형 컬럼은 건드리지 않는다
    assert str(X_train["duration_sec"].dtype) == "float64"


def test_collect_categorical_categories_skips_missing_columns() -> None:
    X_train = pd.DataFrame({"duration_sec": [1.0]})
    X_val = pd.DataFrame({"duration_sec": [2.0]})

    result = collect_categorical_categories(X_train, X_val, ["category_id"])

    assert result == {}
```

- [x] **Step 6: 실패 확인**

```bash
uv run python -m pytest tests/test_pipeline_train.py -v
```

Expected: FAIL — `ImportError: cannot import name 'collect_categorical_categories'`

- [x] **Step 7: train.py 구현**

`src/pipeline/train.py`의 `load_config` 함수 아래에 추가:

```python
def collect_categorical_categories(
    X_train: pd.DataFrame, X_val: pd.DataFrame, categorical_columns: list
) -> dict:
    """
    Categorical 컬럼을 train/val union 카테고리로 캐스팅하고 카테고리 목록을 반환.

    반환된 dict는 categorical_columns.pkl 아티팩트로 저장되어 서빙이 학습과
    동일한 category 코드 매핑을 재현하는 데 사용된다 (src/serving/model_loader.py).
    """
    categories_by_column: dict = {}
    for col in categorical_columns:
        if col not in X_train.columns:
            continue
        categories = pd.api.types.union_categoricals(
            [X_train[col].astype("category"), X_val[col].astype("category")]
        ).categories
        X_train[col] = pd.Categorical(X_train[col], categories=categories)
        X_val[col] = pd.Categorical(X_val[col], categories=categories)
        categories_by_column[col] = categories.tolist()
    return categories_by_column
```

기존 Step 4 블록(`train.py:129-137`)을 다음으로 교체:

```python
        print("\n[Step 4] Categorical 컬럼 dtype 변환...")
        categories_by_column = collect_categorical_categories(
            X_train, X_val, categorical_columns
        )
        print(f"  [OK] {len(categorical_columns)} categorical columns 설정")
```

`main()` 시그니처에 파라미터 추가 (`feature_columns_output` 다음):

```python
    categorical_columns_output: str = None,
```

Step 8 블록에서 `feature_columns_path` 해석 직후에 동일 패턴 추가:

```python
        if categorical_columns_output is None:
            categorical_columns_path = os.path.join(
                project_root, config["artifacts"]["categorical_columns_path"]
            )
        elif not os.path.isabs(categorical_columns_output):
            categorical_columns_path = os.path.join(project_root, categorical_columns_output)
        else:
            categorical_columns_path = categorical_columns_output
```

저장·로깅 블록(`train.py:204-208`)을 다음으로 교체:

```python
        save_model(model.model, model_path)
        save_feature_columns(feature_columns, feature_columns_path)
        save_categorical_columns(categories_by_column, categorical_columns_path)

        # artifact 경로(model/, features/)는 서빙 로더(src/serving/model_loader.py)의
        # MLflow 다운로드 경로 상수와 계약이다 — 변경 시 양쪽을 함께 갱신한다.
        log_artifact(local_path=model_path, artifact_path="model")
        log_artifact(local_path=feature_columns_path, artifact_path="features")
        log_artifact(local_path=categorical_columns_path, artifact_path="features")
```

import 갱신 (`train.py:21`):

```python
from src.utils.model_utils import save_model, save_feature_columns, save_categorical_columns  # noqa: E402
```

말미 출력에 한 줄 추가 (`print(f"Feature columns: ...")` 다음):

```python
    print(f"Categorical columns: {categorical_columns_path}")
```

- [x] **Step 8: config.yaml 갱신**

`src/pipeline/config.yaml`의 `artifacts` 섹션에 추가 (`feature_columns_path` 다음 줄):

```yaml
  categorical_columns_path: artifacts/models/categorical_columns.pkl
```

- [x] **Step 9: cli.py 갱신**

`train_model` 커맨드: `feature_columns_output` 옵션 다음에 추가하고 `train.main(...)` 호출에 전달:

```python
    categorical_columns_output: Optional[str] = typer.Option(None, help="Categorical 카테고리 저장 경로 (config override)"),
```

```python
        categorical_columns_output=categorical_columns_output,
```

`run_pipeline` 커맨드: 동일한 옵션을 추가하고 내부 `train.main(...)` 호출에 동일하게 전달한다. (`evaluate.main`은 변경하지 않는다.)

- [x] **Step 10: 테스트 통과 확인**

```bash
uv run python -m pytest tests/test_pipeline_train.py tests/test_model_utils.py -v
```

Expected: PASS

- [x] **Step 11: Commit**

```bash
git add src/pipeline/config.yaml src/pipeline/train.py src/utils/model_utils.py src/cli.py tests/test_model_utils.py tests/test_pipeline_train.py
git commit -m "feat: 학습 파이프라인에 categorical 카테고리 아티팩트 저장 추가"
```

---

### Task 3: 서빙 — 범주형 아티팩트 로드 및 학습 동일 캐스팅

**Files:**
- Modify: `src/serving/service.py` (Reranker에 `categorical_categories` 필드, 캐스팅 로직 교체)
- Modify: `src/serving/model_loader.py` (아티팩트 경로 상수, 로컬/MLflow 로드, 검증)
- Modify: `tests/test_serving_api.py`
- Modify: `.env.example`, `docs/specs/2026-07-16-reranking-serving-api.md`

**Interfaces:**
- Consumes: Task 2의 `categorical_columns.pkl` (`dict[str, list]`, 컬럼명 → 학습 카테고리 순서 보존)
- Produces: `Reranker(model: ProbabilityModel, feature_columns: tuple[str, ...], categorical_categories: Mapping[str, tuple[FeatureValue, ...]])` — Task 4·6이 이 시그니처를 그대로 사용
- Produces: 환경 변수 `RERANK_CATEGORICAL_COLUMNS_PATH` (local 모드 필수), MLflow 상수 3종

- [x] **Step 1: 실패하는 테스트 작성 — 학습 카테고리 순서로 캐스팅**

`tests/test_serving_api.py`에 추가:

```python
class CategoricalCodeModel:
    """category 코드로 점수를 계산해 카테고리 매핑 정확성을 검증하는 모델."""

    def __init__(self) -> None:
        self.received: object = None

    def predict_proba(self, features):
        self.received = features
        codes = features["category_id"].cat.codes.to_numpy(dtype=float)
        scores = codes / 10.0
        return np.column_stack((1.0 - scores, scores))


def test_rerank_casts_categorical_columns_with_training_categories() -> None:
    model = CategoricalCodeModel()
    reranker = Reranker(
        model=model,
        feature_columns=("category_id",),
        categorical_categories={"category_id": (10, 20, 30)},
    )

    response = reranker.rerank(
        [
            CandidateVideo(video_id="video-cat10", features={"category_id": 10}),
            CandidateVideo(video_id="video-cat30", features={"category_id": 30}),
        ]
    )

    # 학습 카테고리 순서(10, 20, 30) 기준 코드: 10 -> 0, 30 -> 2.
    # 요청에 없는 카테고리 20이 있어도 코드가 밀리지 않아야 한다.
    assert str(model.received["category_id"].dtype) == "category"
    assert list(model.received["category_id"].cat.categories) == [10, 20, 30]
    assert [item.video_id for item in response] == ["video-cat30", "video-cat10"]
    assert response[0].ctr_score == 0.2
    assert response[1].ctr_score == 0.0
```

기존 `Reranker(...)` 생성 호출 전부(`test_rerank_orders_candidates_by_ctr_score`, `test_healthcheck_and_metrics_report_ready_model`)에 `categorical_categories={}` 인자를 추가한다.

- [x] **Step 2: 실패 확인**

```bash
uv run python -m pytest tests/test_serving_api.py -v
```

Expected: 신규 테스트 FAIL — `TypeError: Reranker.__init__() got an unexpected keyword argument 'categorical_categories'`

- [x] **Step 3: service.py 구현**

`src/serving/service.py`에서 import를 갱신:

```python
from collections.abc import Mapping, Sequence
```

```python
from src.serving.schemas import CandidateVideo, FeatureValue, RerankedVideo
```

`Reranker` dataclass에 필드 추가:

```python
@dataclass(frozen=True, slots=True)
class Reranker:
    model: ProbabilityModel
    feature_columns: tuple[str, ...]
    categorical_categories: Mapping[str, tuple[FeatureValue, ...]]
```

`rerank()`의 object dtype 휴리스틱 블록(`service.py:51-52`)을 다음으로 교체:

```python
        # 학습 시점 카테고리·순서를 그대로 재현해야 LightGBM category 코드가 일치한다.
        # 학습에 없던 값은 NaN(결측)으로 처리된다.
        for column, categories in self.categorical_categories.items():
            feature_frame[column] = pd.Categorical(
                feature_frame[column], categories=categories
            )
```

`service.py:8`의 pandas import에서 이제 실제 사용이 명확하므로 주석을 정리:

```python
import pandas as pd
```

- [x] **Step 4: 테스트 통과 확인**

```bash
uv run python -m pytest tests/test_serving_api.py -v
```

Expected: PASS (전부)

- [x] **Step 5: 실패하는 테스트 작성 — 로더**

`tests/test_serving_api.py`의 `test_local_model_loader_reads_model_and_feature_columns`를 다음으로 교체:

```python
def test_local_model_loader_reads_model_and_feature_columns(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    categorical_columns_path = tmp_path / "categorical_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)
    with categorical_columns_path.open("wb") as categorical_columns_file:
        pickle.dump({}, categorical_columns_file)

    reranker = load_local_model(
        LocalModelSettings(
            model_path=model_path,
            feature_columns_path=feature_columns_path,
            categorical_columns_path=categorical_columns_path,
        )
    )

    response = reranker.rerank(
        [
            CandidateVideo(video_id="video-low", features={"ranking_signal": 0.1}),
            CandidateVideo(video_id="video-high", features={"ranking_signal": 0.8}),
        ]
    )

    assert [item.video_id for item in response] == ["video-high", "video-low"]
```

신규 테스트 2개 추가:

```python
def test_local_model_loader_rejects_unknown_categorical_columns(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    categorical_columns_path = tmp_path / "categorical_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)
    with categorical_columns_path.open("wb") as categorical_columns_file:
        pickle.dump({"unknown_column": [1, 2]}, categorical_columns_file)

    with pytest.raises(ModelArtifactError):
        load_local_model(
            LocalModelSettings(
                model_path=model_path,
                feature_columns_path=feature_columns_path,
                categorical_columns_path=categorical_columns_path,
            )
        )


def test_local_model_loader_requires_categorical_artifact(tmp_path: Path) -> None:
    model_path = tmp_path / "model.joblib"
    feature_columns_path = tmp_path / "feature_columns.pkl"
    joblib.dump(RankingModel(), model_path)
    with feature_columns_path.open("wb") as feature_columns_file:
        pickle.dump(["ranking_signal"], feature_columns_file)

    with pytest.raises(ModelArtifactError):
        load_local_model(
            LocalModelSettings(
                model_path=model_path,
                feature_columns_path=feature_columns_path,
                categorical_columns_path=tmp_path / "missing.pkl",
            )
        )
```

파일 상단 import에 추가:

```python
import pytest

from src.serving.model_loader import ModelArtifactError
```

(기존 `from src.serving.model_loader import (...)` 블록에 `ModelArtifactError`를 합친다.)

MLflow 테스트의 스텁과 기대 URI를 갱신 — `download_artifacts` 스텁에 분기 추가:

```python
    def download_artifacts(*, artifact_uri: str) -> str:
        downloaded_uris.append(artifact_uri)
        if artifact_uri.endswith("lgbm_model.joblib"):
            return str(model_path)
        if artifact_uri.endswith("categorical_columns.pkl"):
            return str(categorical_columns_path)
        return str(feature_columns_path)
```

테스트 셋업에 categorical 아티팩트 생성 추가:

```python
    categorical_columns_path = tmp_path / "categorical_columns.pkl"
    with categorical_columns_path.open("wb") as categorical_columns_file:
        pickle.dump({}, categorical_columns_file)
```

기대 URI를 다음으로 교체:

```python
    assert downloaded_uris == [
        "runs:/run-123/model/lgbm_model.joblib",
        "runs:/run-123/features/feature_columns.pkl",
        "runs:/run-123/features/categorical_columns.pkl",
    ]
```

- [x] **Step 6: 실패 확인**

```bash
uv run python -m pytest tests/test_serving_api.py -v
```

Expected: 로더 테스트들 FAIL — `TypeError: LocalModelSettings.__init__() got an unexpected keyword argument 'categorical_columns_path'`

- [x] **Step 7: model_loader.py 구현**

상수 추가 (`FEATURE_COLUMNS_ADAPTER` 근처):

```python
FEATURE_COLUMNS_ADAPTER: Final = TypeAdapter(tuple[str, ...])
CATEGORICAL_CATEGORIES_ADAPTER: Final = TypeAdapter(dict[str, tuple[str | int | float | bool, ...]])

# 학습 파이프라인(src/pipeline/train.py Step 8)의 log_artifact 경로와 계약이다.
# 학습 config(src/pipeline/config.yaml artifacts.*) 파일명이 바뀌면 함께 갱신한다.
MLFLOW_MODEL_ARTIFACT_PATH: Final = "model/lgbm_model.joblib"
MLFLOW_FEATURE_COLUMNS_ARTIFACT_PATH: Final = "features/feature_columns.pkl"
MLFLOW_CATEGORICAL_COLUMNS_ARTIFACT_PATH: Final = "features/categorical_columns.pkl"
```

`LocalModelSettings`에 필드 추가:

```python
@dataclass(frozen=True, slots=True)
class LocalModelSettings:
    model_path: Path
    feature_columns_path: Path
    categorical_columns_path: Path
```

`load_model_settings_from_environment()`의 LOCAL 분기에 추가:

```python
        case ModelSource.LOCAL:
            return LocalModelSettings(
                model_path=Path(_required_environment_value("RERANK_MODEL_PATH")),
                feature_columns_path=Path(
                    _required_environment_value("RERANK_FEATURE_COLUMNS_PATH")
                ),
                categorical_columns_path=Path(
                    _required_environment_value("RERANK_CATEGORICAL_COLUMNS_PATH")
                ),
            )
```

`load_local_model` / `load_mlflow_model` 갱신:

```python
def load_local_model(settings: LocalModelSettings) -> Reranker:
    return _load_reranker(
        model_path=settings.model_path,
        feature_columns_path=settings.feature_columns_path,
        categorical_columns_path=settings.categorical_columns_path,
    )


def load_mlflow_model(settings: MlflowModelSettings) -> Reranker:
    mlflow.set_tracking_uri(settings.tracking_uri)
    model_path = Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{settings.run_id}/{MLFLOW_MODEL_ARTIFACT_PATH}"
        )
    )
    feature_columns_path = Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{settings.run_id}/{MLFLOW_FEATURE_COLUMNS_ARTIFACT_PATH}"
        )
    )
    categorical_columns_path = Path(
        mlflow.artifacts.download_artifacts(
            artifact_uri=f"runs:/{settings.run_id}/{MLFLOW_CATEGORICAL_COLUMNS_ARTIFACT_PATH}"
        )
    )
    return _load_reranker(
        model_path=model_path,
        feature_columns_path=feature_columns_path,
        categorical_columns_path=categorical_columns_path,
    )
```

`_load_reranker` 갱신 — 시그니처에 `categorical_columns_path: Path` 추가, 존재 검사 추가, feature 목록 로드 뒤에 categorical 로드·검증 추가:

```python
def _load_reranker(
    model_path: Path, feature_columns_path: Path, categorical_columns_path: Path
) -> Reranker:
    if not model_path.is_file():
        raise ModelArtifactError(reason=f"Model artifact does not exist: {model_path}")
    if not feature_columns_path.is_file():
        raise ModelArtifactError(
            reason=f"Feature-column artifact does not exist: {feature_columns_path}"
        )
    if not categorical_columns_path.is_file():
        raise ModelArtifactError(
            reason=(
                "Categorical-column artifact does not exist: "
                f"{categorical_columns_path} (categorical_columns.pkl을 저장하는 "
                "학습 파이프라인으로 재학습이 필요합니다.)"
            )
        )

    model = joblib.load(model_path)
    if not isinstance(model, ProbabilityModel):
        raise ModelArtifactError(reason="Loaded model does not implement predict_proba.")

    with feature_columns_path.open("rb") as feature_columns_file:
        try:
            feature_columns = FEATURE_COLUMNS_ADAPTER.validate_python(
                pickle.load(feature_columns_file)
            )
        except ValidationError as error:
            raise ModelArtifactError(
                reason="Feature-column artifact must contain a sequence of strings."
            ) from error

    if not feature_columns:
        raise ModelArtifactError(reason="Feature-column artifact must not be empty.")

    with categorical_columns_path.open("rb") as categorical_columns_file:
        try:
            categorical_categories = CATEGORICAL_CATEGORIES_ADAPTER.validate_python(
                pickle.load(categorical_columns_file)
            )
        except ValidationError as error:
            raise ModelArtifactError(
                reason="Categorical-column artifact must map column names to category lists."
            ) from error

    unknown_columns = tuple(
        column for column in categorical_categories if column not in feature_columns
    )
    if unknown_columns:
        raise ModelArtifactError(
            reason=(
                "Categorical-column artifact has columns outside the feature set: "
                f"{', '.join(unknown_columns)}"
            )
        )

    return Reranker(
        model=model,
        feature_columns=feature_columns,
        categorical_categories=categorical_categories,
    )
```

- [x] **Step 8: 테스트 통과 확인**

```bash
uv run python -m pytest tests/test_serving_api.py -v
```

Expected: PASS (전부)

- [x] **Step 9: 문서 갱신**

`.env.example`의 local 모드 블록에 추가 (`RERANK_FEATURE_COLUMNS_PATH=` 다음 줄):

```
RERANK_CATEGORICAL_COLUMNS_PATH=
```

`docs/specs/2026-07-16-reranking-serving-api.md`의 "모델 artifact" 섹션을 다음으로 교체:

```markdown
## 모델 artifact

- `RERANK_MODEL_SOURCE=local`: `RERANK_MODEL_PATH`의 joblib/pickle 모델,
  `RERANK_FEATURE_COLUMNS_PATH`의 pickle feature 목록,
  `RERANK_CATEGORICAL_COLUMNS_PATH`의 pickle 범주형 카테고리 dict를 로드한다.
- `RERANK_MODEL_SOURCE=mlflow`: `MLFLOW_TRACKING_URI`와 `RERANK_MLFLOW_RUN_ID`를 사용해
  `runs:/<run_id>/model/lgbm_model.joblib`, `runs:/<run_id>/features/feature_columns.pkl`,
  `runs:/<run_id>/features/categorical_columns.pkl` artifact를 내려받아 로드한다.

`categorical_columns.pkl`은 `dict[컬럼명, 카테고리 리스트]`이며 학습 시점
카테고리 값·순서를 보존한다. 서빙은 이 목록으로 `pd.Categorical`을 구성해
LightGBM category 코드 매핑을 학습과 동일하게 재현한다. 요청 feature 값의
타입은 학습 데이터와 동일해야 하며(예: 학습이 int였다면 int로 전송), 학습에
없던 카테고리 값은 결측(NaN)으로 처리된다. 이 아티팩트는 필수다 — 없는 기존
run은 재학습이 필요하다.

현재 학습 파이프라인의 artifact 경로와 일치한다(경로 상수는
`src/serving/model_loader.py`와 `src/pipeline/train.py`가 계약으로 공유).
MLflow registry alias를 통한 pyfunc 모델 로드는 학습 파이프라인이 MLflow
model flavor를 기록하도록 확장될 때 별도 작업으로 다룬다.
```

- [x] **Step 10: 전체 테스트**

```bash
uv run python -m pytest -v
```

Expected: 전체 PASS

- [x] **Step 11: Commit**

```bash
git add src/serving/service.py src/serving/model_loader.py tests/test_serving_api.py .env.example docs/specs/2026-07-16-reranking-serving-api.md
git commit -m "fix: 서빙 categorical 캐스팅을 학습 카테고리 아티팩트 기반으로 교체"
```

---

### Task 4: 예측 예외를 PredictionError로 매핑

**Files:**
- Modify: `src/serving/service.py` (`rerank()`의 `predict_proba` 호출)
- Test: `tests/test_serving_api.py`

**Interfaces:**
- Consumes: Task 3의 `Reranker` 시그니처
- Produces: `predict_proba`가 던지는 모든 예외 → `PredictionError` → API 500 + 고정 메시지 (`app.py`의 기존 매핑 재사용, app.py 변경 없음)

- [x] **Step 1: 실패하는 테스트 작성**

`tests/test_serving_api.py`에 추가:

```python
class RaisingModel:
    def predict_proba(self, features):
        raise ValueError("boom")


def test_rerank_returns_500_when_model_raises() -> None:
    reranker = Reranker(
        model=RaisingModel(),
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )
    app = create_app(reranker=reranker)

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={
                "user_id": "user-1",
                "candidates": [
                    {"video_id": "video-1", "features": {"ranking_signal": 0.5}},
                ],
            },
        )

    assert response.status_code == 500
    assert response.json() == {"detail": "Reranking model returned an invalid prediction."}
```

- [x] **Step 2: 실패 확인**

```bash
uv run python -m pytest tests/test_serving_api.py::test_rerank_returns_500_when_model_raises -v
```

Expected: FAIL — `ValueError: boom`이 그대로 전파 (TestClient가 raise)

- [x] **Step 3: service.py 구현**

`rerank()`의 `predict_proba` 호출을 다음으로 교체:

```python
        try:
            probabilities = self.model.predict_proba(feature_frame)
        except Exception as error:
            raise PredictionError(reason="Model prediction raised an exception.") from error
```

- [x] **Step 4: 테스트 통과 확인**

```bash
uv run python -m pytest tests/test_serving_api.py -v
```

Expected: PASS (전부)

- [x] **Step 5: Commit**

```bash
git add src/serving/service.py tests/test_serving_api.py
git commit -m "fix: 서빙 예측 중 발생하는 예외를 PredictionError로 매핑"
```

---

### Task 5: rerank_candidates 히스토그램 버킷을 후보 수 스케일로 지정

**Files:**
- Modify: `src/serving/app.py:20`
- Test: `tests/test_serving_api.py`

**Interfaces:**
- Consumes: 없음 (독립)
- Produces: `/metrics`에 `rerank_candidates_bucket{le="50.0"}` 등 후보 수 스케일 버킷 노출

- [x] **Step 1: 실패하는 테스트 작성**

`tests/test_serving_api.py`에 추가:

```python
def test_metrics_expose_candidate_count_buckets() -> None:
    reranker = Reranker(
        model=RankingModel(),
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )
    app = create_app(reranker=reranker)

    with TestClient(app) as client:
        client.post(
            "/rerank",
            json={
                "user_id": "user-1",
                "candidates": [
                    {"video_id": "video-1", "features": {"ranking_signal": 0.5}},
                ],
            },
        )
        metrics_response = client.get("/metrics")

    assert 'rerank_candidates_bucket{le="50.0"}' in metrics_response.text
    assert 'rerank_candidates_bucket{le="500.0"}' in metrics_response.text
```

- [x] **Step 2: 실패 확인**

```bash
uv run python -m pytest tests/test_serving_api.py::test_metrics_expose_candidate_count_buckets -v
```

Expected: FAIL — 기본 버킷(0.005~10.0)에는 `le="50.0"` 없음

- [x] **Step 3: app.py 구현**

`src/serving/app.py:20`을 다음으로 교체:

```python
RERANK_CANDIDATES = Histogram(
    "rerank_candidates",
    "Candidate count per reranking request.",
    buckets=(1, 2, 5, 10, 20, 50, 100, 200, 500),
)
```

- [x] **Step 4: 테스트 통과 확인**

```bash
uv run python -m pytest tests/test_serving_api.py -v
```

Expected: PASS (전부)

- [x] **Step 5: Commit**

```bash
git add src/serving/app.py tests/test_serving_api.py
git commit -m "fix: rerank_candidates 히스토그램 버킷을 후보 수 스케일로 지정"
```

---

### Task 6: missing-feature 검사 단일 순회로 간소화 + 422 테스트 보강

**Files:**
- Modify: `src/serving/service.py` (`rerank()`의 missing 검사)
- Test: `tests/test_serving_api.py`

**Interfaces:**
- Consumes: Task 3·4가 완성한 `rerank()` 본문
- Produces: 동작 동일(누락 컬럼은 `feature_columns` 순서로 보고), O(컬럼×후보) → O(컬럼+후보 키 합)

- [x] **Step 1: 실패하는 테스트 작성 (동작 고정용 — 현재 구현에서도 통과해야 함)**

`tests/test_serving_api.py`에 추가:

```python
def test_rerank_returns_422_when_candidate_missing_feature() -> None:
    reranker = Reranker(
        model=RankingModel(),
        feature_columns=("ranking_signal",),
        categorical_categories={},
    )
    app = create_app(reranker=reranker)

    with TestClient(app) as client:
        response = client.post(
            "/rerank",
            json={
                "user_id": "user-1",
                "candidates": [
                    {"video_id": "video-1", "features": {"ranking_signal": 0.5}},
                    {"video_id": "video-2", "features": {"other": 1.0}},
                ],
            },
        )

    assert response.status_code == 422
    assert response.json() == {"detail": "Missing required model features: ranking_signal"}
```

- [x] **Step 2: 현재 구현에서 통과 확인 (리팩터링 안전망)**

```bash
uv run python -m pytest tests/test_serving_api.py::test_rerank_returns_422_when_candidate_missing_feature -v
```

Expected: PASS — 이 테스트는 리팩터링 전후 동작 불변을 보증한다.

- [x] **Step 3: service.py 리팩터링**

`rerank()`의 missing 검사 블록(이중 순회)을 다음으로 교체:

```python
        if not candidates:
            return []

        common_keys = set(candidates[0].features)
        for candidate in candidates[1:]:
            common_keys &= candidate.features.keys()
        missing_columns = tuple(
            column for column in self.feature_columns if column not in common_keys
        )
        if missing_columns:
            raise MissingFeatureColumnsError(columns=missing_columns)
```

(빈 `candidates`는 API 스키마(`min_length=1`)가 막지만, `rerank()` 직접 호출을 `candidates[0]` IndexError로부터 보호한다.)

- [x] **Step 4: 테스트 통과 확인**

```bash
uv run python -m pytest tests/test_serving_api.py -v
```

Expected: PASS (전부)

- [x] **Step 5: Commit**

```bash
git add src/serving/service.py tests/test_serving_api.py
git commit -m "refactor: missing feature 검사를 단일 순회로 간소화"
```

---

### 최종 검증 (전체 태스크 완료 후)

- [x] **전체 테스트 (CI 동일 호출)**

```bash
uv sync --frozen && uv run --no-sync python -m pytest -v
```

Expected: 전체 PASS

- [x] **린트**

```bash
uv run --no-sync ruff check src tests
```

Expected: 오류 없음 (repo lint job은 `autoresearch tests` 대상이지만 serving 변경은 `src`도 확인)

- [x] **serving 이미지 빌드 검증** (docker 가용 시)

```bash
docker build -f deploy/serving/Dockerfile -t autoresearch-serving:review-fix .
```

Expected: 빌드 성공

- [x] **후속 이슈 제안 (구현 금지, 보고만)**: `src/pipeline/evaluate.py:78-80`이 test set 단독 `astype("category")`를 사용해 학습과 category 코드가 어긋날 수 있음 — `categorical_columns.pkl` 아티팩트를 로드하도록 고치는 별도 이슈를 사용자에게 제안한다.
