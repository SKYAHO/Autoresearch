# 일일 추천 결과 BQ 적재 배치 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `models:/ctr-model@champion` 모델로 일일 트렌딩 후보를 가상 유저 전원에 대해 채점해 유저별 전체 순위를 BigQuery `user_recommendations` 파티션 테이블에 멱등 적재하는 배치를 만든다.

**Architecture:** `src/serving/model_loader.py`에 registry(alias) 소스를 확장해 계보(run_id·버전)를 함께 반환하고, BQ 가상 유저 → 학습 계약 personas 변환을 정식 어댑터로 승격한다. `src/pipeline/daily_recommendations.py`는 기존 `simulate_policy_round.build_pool_feature_frame`과 `_to_candidate_videos`, `derive_wide_events`를 그대로 재사용해 학습·정책 라운드와 피처 계산 경로를 공유하고, 파티션 데코레이터 + `WRITE_TRUNCATE`로 결과를 적재한다. 공개 CLI는 batch-contract-v1 JSON/exit 계약을 따르며 `Dockerfile.app`에 `src/`를 포함한다. Source spec: `docs/specs/2026-07-21-daily-recommendations-batch.md` (`bec2f53`).

**Tech Stack:** Python 3.12, pandas, google-cloud-bigquery(+db-dtypes), mlflow(MlflowClient), LightGBM(기존 아티팩트), pytest.

## Global Constraints

- 브랜치: `feat/216-daily-recommendations-batch` (이슈 #216에서 생성된 현재 브랜치).
- 한국어 격식체 docstring, 모든 함수 타입 힌트(반환 포함).
- 기존 `local`/`mlflow` 소스의 동작·시그니처 불변. `load_reranker` 시그니처 불변.
- action log는 **단일 dt 파티션**에서만 소비한다 (파티션 간 UNION 금지 — spec 결정 4).
- 피처 조립은 기존 `simulate_policy_round.build_pool_feature_frame`을 호출해 `src/features/assembly.py` 공용 계산 경로를 재사용한다 (배치 안에서 재구현 금지 — 스큐 방지).
- 멱등 적재: 파티션 데코레이터(`<table>$YYYYMMDD`) + `WRITE_TRUNCATE`.
- rank 결정론: ctr_score 내림차순, 동점은 video_id 오름차순.
- 테스트는 실 BQ·MLflow에 접속하지 않는다 (stub/fake 주입).
- 테스트 명령: `uv run python -m pytest tests/<파일>.py -v`. 신규 모듈에는 archmap 사이드카(`__arch__`) 선언을 포함한다 (#202 게이트).
- 공개 CLI는 `--help`/`--version`, stdout 단일 `job_summary` JSON, exit 0/1/2 계약을 지킨다. 진단 로그는 stderr로만 보낸다.
- 테이블 환경변수는 기존 prefix를 확장한 `CTR_TRAINING_BQ_VIRTUAL_USERS_TABLE`과 `CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE`을 사용한다.
- 의존성 변경 절차: `pyproject.toml` 수정 → `uv lock` → `uv sync` (CLAUDE.md).

## 파일 구조 (최종)

```
src/serving/model_loader.py              # 수정: REGISTRY 소스, ResolvedModel, load_reranker_with_lineage (Task 1)
src/pipeline/virtual_user_adapter.py     # 신규: BQ 가상 유저 → personas 계약 변환 (Task 2)
src/pipeline/daily_recommendations.py    # 신규: 순위 조립·적재 헬퍼(Task 3) + 배치 main/CLI(Task 4)
pyproject.toml, uv.lock                  # 수정: db-dtypes 추가 (Task 4)
docs/specs/2026-07-13-public-batch-execution-contract.md  # 수정: 명령 등재 (Task 5)
Dockerfile.app                           # 수정: 공개 명령 실행에 필요한 src/ 포함 (Task 5)
.github/workflows/ci.yml                 # 수정: 이미지 --help/--version smoke (Task 5)
.github/workflows/release.yml            # 수정: 발행 digest 공개 명령 검증 (Task 5)
tests/test_serving_model_registry.py     # 신규 (Task 1)
tests/test_virtual_user_adapter.py       # 신규 (Task 2)
tests/test_daily_recommendations.py      # 신규 (Task 3, 4, 5)
tests/test_release_workflow.py           # 수정: 이미지에 daily 명령 포함 계약 (Task 5)
```

---

### Task 1: registry 소스 확장 (`src/serving/model_loader.py`)

**Files:**
- Modify: `src/serving/model_loader.py` (ModelSource enum, 설정 dataclass, env 파서, 로더)
- Test: `tests/test_serving_model_registry.py` (신규)

**Interfaces:**
- Consumes: 기존 `MlflowModelSettings`, `load_mlflow_model`, `load_local_model`, `ModelConfigurationError`, `ModelArtifactError`, `_required_environment_value`
- Produces (Task 4가 사용):
  - `ModelSource.REGISTRY = "registry"`
  - `@dataclass(frozen=True, slots=True) class RegistryModelSettings: tracking_uri: str; model_name: str; alias: str`
  - `@dataclass(frozen=True, slots=True) class ResolvedModel: reranker: Reranker; run_id: str; model_version: str | None`
  - `load_reranker_with_lineage(settings: ModelSettings) -> ResolvedModel` — local이면 `run_id="local"`, `model_version=None`; mlflow면 `run_id=settings.run_id`, `model_version=None`; registry면 alias 해석 결과.
  - env 계약: `RERANK_MODEL_SOURCE=registry` → `MLFLOW_TRACKING_URI`(필수), `RERANK_REGISTRY_MODEL_NAME`(기본 `ctr-model`), `RERANK_REGISTRY_ALIAS`(기본 `champion`)

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_serving_model_registry.py`

```python
"""registry(alias) 모델 소스 확장 단위 테스트 — 실 MLflow 미접속(stub)."""

from types import SimpleNamespace

import pytest

import src.serving.model_loader as model_loader
from src.serving.model_loader import (
    ModelConfigurationError,
    RegistryModelSettings,
    ResolvedModel,
    load_model_settings_from_environment,
    load_reranker_with_lineage,
)


class _SentinelReranker:
    """로더 위임 검증용 자리표시자 (Reranker 프로토콜 검사 없이 통과)."""


def test_environment_parses_registry_source(monkeypatch):
    monkeypatch.setenv("RERANK_MODEL_SOURCE", "registry")
    monkeypatch.setenv("MLFLOW_TRACKING_URI", "http://mlflow:5000")
    monkeypatch.delenv("RERANK_REGISTRY_MODEL_NAME", raising=False)
    monkeypatch.delenv("RERANK_REGISTRY_ALIAS", raising=False)
    settings = load_model_settings_from_environment()
    assert settings == RegistryModelSettings(
        tracking_uri="http://mlflow:5000", model_name="ctr-model", alias="champion"
    )


def test_environment_registry_requires_tracking_uri(monkeypatch):
    monkeypatch.setenv("RERANK_MODEL_SOURCE", "registry")
    monkeypatch.delenv("MLFLOW_TRACKING_URI", raising=False)
    with pytest.raises(ModelConfigurationError):
        load_model_settings_from_environment()


def test_registry_resolves_alias_and_reuses_run_download(monkeypatch):
    calls = {}

    class _FakeClient:
        def get_model_version_by_alias(self, name, alias):
            calls["alias"] = (name, alias)
            return SimpleNamespace(run_id="run-abc", version=7)

    sentinel = _SentinelReranker()

    def _fake_load_mlflow_model(settings):
        calls["mlflow_settings"] = settings
        return sentinel

    monkeypatch.setattr(model_loader, "MlflowClient", lambda: _FakeClient())
    monkeypatch.setattr(model_loader, "load_mlflow_model", _fake_load_mlflow_model)

    resolved = load_reranker_with_lineage(
        RegistryModelSettings(
            tracking_uri="http://mlflow:5000", model_name="ctr-model", alias="champion"
        )
    )
    assert isinstance(resolved, ResolvedModel)
    assert resolved.reranker is sentinel
    assert (resolved.run_id, resolved.model_version) == ("run-abc", "7")
    assert calls["alias"] == ("ctr-model", "champion")
    assert calls["mlflow_settings"].run_id == "run-abc"
    assert calls["mlflow_settings"].tracking_uri == "http://mlflow:5000"


def test_registry_alias_failure_maps_to_artifact_error(monkeypatch):
    class _BrokenClient:
        def get_model_version_by_alias(self, name, alias):
            raise RuntimeError("registry unavailable")

    monkeypatch.setattr(model_loader, "MlflowClient", lambda: _BrokenClient())
    with pytest.raises(model_loader.ModelArtifactError):
        load_reranker_with_lineage(
            RegistryModelSettings(
                tracking_uri="http://mlflow:5000", model_name="ctr-model", alias="champion"
            )
        )


def test_lineage_for_mlflow_and_local_sources(monkeypatch, tmp_path):
    sentinel = _SentinelReranker()
    monkeypatch.setattr(model_loader, "load_mlflow_model", lambda s: sentinel)
    monkeypatch.setattr(model_loader, "load_local_model", lambda s: sentinel)

    from src.serving.model_loader import LocalModelSettings, MlflowModelSettings

    mlflow_resolved = load_reranker_with_lineage(
        MlflowModelSettings(tracking_uri="http://mlflow:5000", run_id="run-z")
    )
    assert (mlflow_resolved.run_id, mlflow_resolved.model_version) == ("run-z", None)

    local_resolved = load_reranker_with_lineage(
        LocalModelSettings(
            model_path=tmp_path / "m.joblib",
            feature_columns_path=tmp_path / "f.pkl",
            categorical_columns_path=tmp_path / "c.pkl",
        )
    )
    assert (local_resolved.run_id, local_resolved.model_version) == ("local", None)
```

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_serving_model_registry.py -v`
Expected: FAIL — `ImportError: cannot import name 'RegistryModelSettings'`

- [ ] **Step 3: 구현** — `src/serving/model_loader.py`

import에 추가 (`import mlflow` 아래):

```python
from mlflow.tracking import MlflowClient
```

`ModelSource`에 값 추가:

```python
class ModelSource(StrEnum):
    """모델 아티팩트를 어디서 읽을지 지정하는 소스 종류(로컬 파일 / MLflow 런 / Registry alias)."""

    LOCAL = "local"
    MLFLOW = "mlflow"
    REGISTRY = "registry"
```

`MlflowModelSettings` 아래에 dataclass 2개 추가, `ModelSettings` 확장:

```python
@dataclass(frozen=True, slots=True)
class RegistryModelSettings:
    """Model Registry alias(예: models:/ctr-model@champion)로 로드할 때 필요한 설정."""

    tracking_uri: str
    model_name: str
    alias: str


ModelSettings: TypeAlias = LocalModelSettings | MlflowModelSettings | RegistryModelSettings


@dataclass(frozen=True, slots=True)
class ResolvedModel:
    """로드된 Reranker와 계보(run_id·Registry 버전)를 함께 담는다.

    local 소스는 run_id="local", registry가 아니면 model_version=None이다.
    """

    reranker: Reranker
    run_id: str
    model_version: str | None
```

`load_model_settings_from_environment`의 `match source:`에 case 추가 (`case ModelSource.MLFLOW:` 뒤):

```python
        case ModelSource.REGISTRY:
            return RegistryModelSettings(
                tracking_uri=_required_environment_value("MLFLOW_TRACKING_URI"),
                model_name=os.getenv("RERANK_REGISTRY_MODEL_NAME", "ctr-model"),
                alias=os.getenv("RERANK_REGISTRY_ALIAS", "champion"),
            )
```

(에러 메시지도 갱신: `"RERANK_MODEL_SOURCE must be 'local', 'mlflow', or 'registry'."`)

`load_mlflow_model` 아래에 추가:

```python
def _load_registry_model(settings: RegistryModelSettings) -> ResolvedModel:
    """Registry alias를 run_id로 해석한 뒤 기존 run 아티팩트 다운로드 경로를 재사용한다."""
    mlflow.set_tracking_uri(settings.tracking_uri)
    try:
        version = MlflowClient().get_model_version_by_alias(settings.model_name, settings.alias)
    except Exception as error:
        raise ModelArtifactError(
            reason=(
                f"Failed to resolve registry alias models:/{settings.model_name}"
                f"@{settings.alias}: {error}"
            )
        ) from error
    reranker = load_mlflow_model(
        MlflowModelSettings(tracking_uri=settings.tracking_uri, run_id=version.run_id)
    )
    return ResolvedModel(
        reranker=reranker, run_id=version.run_id, model_version=str(version.version)
    )


def load_reranker_with_lineage(settings: ModelSettings) -> ResolvedModel:
    """설정 종류에 따라 로드하고 계보(run_id·버전)를 함께 반환한다."""
    match settings:
        case RegistryModelSettings():
            return _load_registry_model(settings)
        case MlflowModelSettings():
            return ResolvedModel(
                reranker=load_mlflow_model(settings), run_id=settings.run_id, model_version=None
            )
        case LocalModelSettings():
            return ResolvedModel(
                reranker=load_local_model(settings), run_id="local", model_version=None
            )
        case unreachable:
            assert_never(unreachable)
```

`load_reranker`의 match에도 case 추가 (시그니처 불변 — registry 설정이 오면 Reranker만 반환):

```python
        case RegistryModelSettings():
            return _load_registry_model(settings).reranker
```

`src/serving/model_loader.py`에는 현재 `__arch__`가 없지만 이번 작업이 공개 심볼을 추가하므로 archmap stale 판정을 피하려면 다음 사이드카도 함께 추가한다.

```python
__arch__ = {
    "stage": "serving",
    "role": "로컬 파일·MLflow run·Registry alias에서 Reranker 아티팩트를 해석합니다.",
    "owns": ["모델 소스 설정 파싱", "아티팩트 로드", "Registry alias 계보 해석"],
    "not_owns": ["모델 학습", "추천 후보 피처 조립"],
}
```

- [ ] **Step 4: 통과 확인 + 기존 서빙 회귀 확인**

Run: `uv run python -m pytest tests/test_serving_model_registry.py tests/test_serving_api.py -v`
Expected: 전부 PASS (기존 local/mlflow 경로 무수정 통과)

- [ ] **Step 5: 커밋**

```bash
git add src/serving/model_loader.py tests/test_serving_model_registry.py
git commit -m "feat: model_loader에 Registry alias 소스와 계보 반환 추가 (#216)

RERANK_MODEL_SOURCE=registry로 models:/<name>@<alias>를 run_id로 해석해
기존 run 아티팩트 경로를 재사용한다. 기존 local/mlflow 동작 불변."
```

---

### Task 2: 가상 유저 어댑터 (`src/pipeline/virtual_user_adapter.py`)

**Files:**
- Create: `src/pipeline/virtual_user_adapter.py`
- Test: `tests/test_virtual_user_adapter.py`

**Interfaces:**
- Produces (Task 4가 사용):
  - `extract_words(value: object) -> list[str]` — BQ Arrow 중첩(`{'list': [{'element': str}, ...]}`)·평평한 시퀀스·None 모두 처리
  - `to_personas_frame(virtual_users: pd.DataFrame) -> pd.DataFrame` — 입력 필수 컬럼 `user_id, age, occupation, hobby_keywords, interest_keywords, lifestyle_keywords` → 출력 컬럼 `uuid, age, occupation, hobbies_and_interests_list(JSON 문자열), hobbies_and_interests(", " join 텍스트)`

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_virtual_user_adapter.py`

```python
"""BQ 가상 유저 → 학습 계약 personas 어댑터 단위 테스트 (v2 모델 결함 회귀 테스트)."""

import json

import numpy as np
import pandas as pd

from src.pipeline.virtual_user_adapter import extract_words, to_personas_frame


def test_extract_words_handles_bq_arrow_nested_structure():
    # 2026-07-21 실측 구조: 중첩 dict를 그대로 순회하면 키 "list"만 나와 키워드가 붕괴된다.
    nested = {"list": np.array([{"element": "노포 맛집 탐방"}, {"element": "리그 오브 레전드"}], dtype=object)}
    assert extract_words(nested) == ["노포 맛집 탐방", "리그 오브 레전드"]


def test_extract_words_handles_flat_and_empty_inputs():
    assert extract_words(["글램핑", "드라이브"]) == ["글램핑", "드라이브"]
    assert extract_words(np.array(["산책"], dtype=object)) == ["산책"]
    assert extract_words(None) == []
    assert extract_words({"list": np.array([], dtype=object)}) == []
    assert extract_words(123) == []  # 비순회 스칼라는 빈 목록


def test_to_personas_frame_builds_training_contract_columns():
    vu = pd.DataFrame(
        {
            "user_id": ["vu_0001"],
            "age": [24],
            "occupation": ["대학생"],
            "hobby_keywords": [{"list": np.array([{"element": "게임"}], dtype=object)}],
            "interest_keywords": [["e스포츠"]],
            "lifestyle_keywords": [None],
        }
    )
    personas = to_personas_frame(vu)
    assert list(personas.columns) == [
        "uuid", "age", "occupation", "hobbies_and_interests_list", "hobbies_and_interests",
    ]
    row = personas.iloc[0]
    assert row.uuid == "vu_0001"
    assert json.loads(row.hobbies_and_interests_list) == ["게임", "e스포츠"]
    assert row.hobbies_and_interests == "게임, e스포츠"


def test_to_personas_frame_keeps_users_with_no_keywords():
    vu = pd.DataFrame(
        {
            "user_id": ["vu_0002"],
            "age": [50],
            "occupation": ["자영업"],
            "hobby_keywords": [None],
            "interest_keywords": [None],
            "lifestyle_keywords": [None],
        }
    )
    personas = to_personas_frame(vu)
    assert json.loads(personas.iloc[0].hobbies_and_interests_list) == []
    assert personas.iloc[0].hobbies_and_interests == ""
```

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_virtual_user_adapter.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.pipeline.virtual_user_adapter'`

- [ ] **Step 3: 구현** — `src/pipeline/virtual_user_adapter.py`

```python
"""BQ 가상 유저 테이블을 학습·피처 조립 계약의 personas DataFrame으로 변환한다.

BigQuery REPEATED 컬럼은 클라이언트에서 Arrow 중첩 구조
({'list': [{'element': str}, ...]})로 내려올 수 있다. 이를 그대로 순회하면
dict 키("list")만 추출되어 전 키워드가 붕괴된다 — 2026-07-21 v2 모델 결함의
원인이었으며, 이 모듈이 그 정규화를 단일 책임으로 가진다.
"""

from __future__ import annotations

__arch__ = {
    "stage": "training",
    "role": "BQ 가상 유저 테이블을 학습 계약 personas 형태로 정규화합니다.",
    "owns": [
        "BQ Arrow 중첩 배열 키워드 추출",
        "personas 계약 컬럼(uuid·age·occupation·관심사) 조립",
    ],
    "not_owns": [
        "피처 계산",
        "BigQuery 조회 실행",
    ],
}

import json

import pandas as pd

_KEYWORD_COLUMNS = ("hobby_keywords", "interest_keywords", "lifestyle_keywords")


def extract_words(value: object) -> list[str]:
    """키워드 컬럼 값에서 단어 목록을 추출한다.

    지원 형태: BQ Arrow 중첩({'list': [{'element': str}, ...]}), 평평한
    시퀀스(list/ndarray), None. 그 외 비순회 값은 빈 목록으로 취급한다.
    """
    if value is None:
        return []
    if isinstance(value, dict) and "list" in value:
        words: list[str] = []
        for entry in value["list"]:
            word = entry.get("element") if isinstance(entry, dict) else entry
            if word:
                words.append(str(word))
        return words
    if isinstance(value, str):
        return [value] if value else []
    try:
        return [str(word) for word in value if word]
    except TypeError:
        return []


def to_personas_frame(virtual_users: pd.DataFrame) -> pd.DataFrame:
    """가상 유저 테이블을 personas 계약(uuid/age/occupation/관심사 2형)으로 변환한다."""
    word_lists = virtual_users.apply(
        lambda row: [
            word for column in _KEYWORD_COLUMNS for word in extract_words(row[column])
        ],
        axis=1,
    )
    return pd.DataFrame(
        {
            "uuid": virtual_users["user_id"].astype(str),
            "age": virtual_users["age"],
            "occupation": virtual_users["occupation"],
            "hobbies_and_interests_list": word_lists.apply(
                lambda words: json.dumps(words, ensure_ascii=False)
            ),
            "hobbies_and_interests": word_lists.apply(", ".join),
        }
    )
```

- [ ] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_virtual_user_adapter.py -v`
Expected: PASS (4건)

- [ ] **Step 5: 커밋**

```bash
git add src/pipeline/virtual_user_adapter.py tests/test_virtual_user_adapter.py
git commit -m "feat: BQ 가상 유저 -> personas 계약 어댑터 정식화 (#216)

Arrow 중첩 배열 미처리로 전 키워드가 'list'로 붕괴하던 결함(v2 모델
원인)의 회귀 테스트 포함."
```

---

### Task 3: 순위 행 조립·멱등 적재 헬퍼 (`src/pipeline/daily_recommendations.py` 1/2)

**Files:**
- Create: `src/pipeline/daily_recommendations.py` (헬퍼까지 — main/CLI는 Task 4)
- Test: `tests/test_daily_recommendations.py` (신규)

**Interfaces:**
- Consumes: `src.serving.schemas.RerankedVideo(video_id: str, ctr_score: float)`
- Produces (Task 4가 사용):
  - `parse_iso8601_duration(value: object) -> int`
  - `to_recommendation_rows(user_id, ranked, *, dt, events_dt, model_run_id, model_version, generated_at) -> list[dict]` — 동점 tie-break 포함 rank 1..N
  - `RECOMMENDATIONS_SCHEMA: list[bigquery.SchemaField]` (9컬럼, spec 테이블 계약)
  - `ensure_output_table(client, table_id) -> None` — dt DAY 파티션 테이블 없으면 생성(exists_ok)
  - `write_partition(client, table_id, frame, dt) -> None` — `<table>$YYYYMMDD` + WRITE_TRUNCATE

- [ ] **Step 1: 실패하는 테스트 작성** — `tests/test_daily_recommendations.py`

```python
"""일일 추천 배치 헬퍼 단위 테스트 — 실 BQ 미접속(fake client)."""

from datetime import UTC, date, datetime

import pandas as pd

from src.pipeline.daily_recommendations import (
    RECOMMENDATIONS_SCHEMA,
    ensure_output_table,
    parse_iso8601_duration,
    to_recommendation_rows,
    write_partition,
)
from src.serving.schemas import RerankedVideo

_GENERATED_AT = datetime(2026, 7, 21, 12, 0, 0, tzinfo=UTC)


def _rows(ranked):
    return to_recommendation_rows(
        "vu_0001",
        ranked,
        dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        model_run_id="run-abc",
        model_version="3",
        generated_at=_GENERATED_AT,
    )


def test_parse_iso8601_duration():
    assert parse_iso8601_duration("PT4M29S") == 269
    assert parse_iso8601_duration("PT1H2M3S") == 3723
    assert parse_iso8601_duration(None) == 0
    assert parse_iso8601_duration("not-a-duration") == 0
    assert parse_iso8601_duration("PT1Mgarbage") == 0


def test_rows_rank_descending_with_video_id_tiebreak():
    ranked = [
        RerankedVideo(video_id="vB", ctr_score=0.9),
        RerankedVideo(video_id="vC", ctr_score=0.5),
        RerankedVideo(video_id="vA", ctr_score=0.9),  # vB와 동점 → video_id 오름차순으로 앞
    ]
    rows = _rows(ranked)
    assert [(r["rank"], r["video_id"]) for r in rows] == [(1, "vA"), (2, "vB"), (3, "vC")]
    first = rows[0]
    assert first["user_id"] == "vu_0001"
    assert first["dt"] == date(2026, 7, 21)
    assert first["model_run_id"] == "run-abc"
    assert first["model_version"] == "3"
    assert first["generated_at"] == _GENERATED_AT


class _FakeLoadJob:
    def result(self):
        return None


class _FakeClient:
    def __init__(self):
        self.created = []
        self.loads = []
        self.partitions = {}

    def create_table(self, table, exists_ok=False):
        self.created.append((table, exists_ok))

    def load_table_from_dataframe(self, frame, destination, job_config=None):
        self.loads.append((frame.copy(), destination, job_config))
        if job_config.write_disposition == "WRITE_TRUNCATE":
            self.partitions[destination] = frame.copy()
        return _FakeLoadJob()


def test_ensure_output_table_creates_partitioned_table_exists_ok():
    client = _FakeClient()
    ensure_output_table(client, "proj.ds.user_recommendations")
    (table, exists_ok), = client.created
    assert exists_ok is True
    assert table.time_partitioning.field == "dt"
    assert [f.name for f in table.schema] == [f.name for f in RECOMMENDATIONS_SCHEMA]


def test_write_partition_is_idempotent_by_truncate_decorator():
    client = _FakeClient()
    frame = pd.DataFrame({"user_id": ["u1"], "video_id": ["v1"]})
    for _ in range(2):  # 같은 dt 2회 실행 = 같은 파티션 대상 + TRUNCATE → 중복 불가능
        write_partition(client, "proj.ds.user_recommendations", frame, date(2026, 7, 21))
    destinations = [dest for _, dest, _ in client.loads]
    dispositions = [cfg.write_disposition for _, _, cfg in client.loads]
    assert destinations == ["proj.ds.user_recommendations$20260721"] * 2
    assert dispositions == ["WRITE_TRUNCATE"] * 2
    assert len(client.partitions["proj.ds.user_recommendations$20260721"]) == 1
```

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_daily_recommendations.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.pipeline.daily_recommendations'`

- [ ] **Step 3: 구현** — `src/pipeline/daily_recommendations.py` (헬퍼 부분)

```python
"""일일 추천 결과 BQ 적재 배치.

champion 모델(models:/ctr-model@champion)로 일일 트렌딩 후보 전체를 가상 유저
전원에 대해 채점해, 유저별 전체 순위를 user_recommendations 파티션 테이블에
멱등 적재한다. 비교 실험·노출 선정은 이 배치의 책임이 아니다(spec 참조).

spec: docs/specs/2026-07-21-daily-recommendations-batch.md
"""

from __future__ import annotations

__arch__ = {
    "stage": "serving",
    "role": "champion 모델로 일일 후보를 채점해 유저별 순위를 BigQuery에 적재합니다.",
    "owns": [
        "일일 추천 순위 산출·계보 태깅",
        "user_recommendations 파티션 멱등 적재",
    ],
    "not_owns": [
        "노출 선정(Top-K + exploration)과 LLM 판정",
        "모델 학습과 Registry alias 운영",
    ],
}

import argparse
import json
import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from typing import Callable, Final, Sequence

import pandas as pd
from google.cloud import bigquery

from autoresearch.jobs import BATCH_CONTRACT_VERSION
from src.pipeline.build_training_dataset import (
    BIGQUERY_DATASET,
    BIGQUERY_PROJECT,
    derive_wide_events,
    load_events_from_bigquery,
)
from src.pipeline.virtual_user_adapter import to_personas_frame
from src.serving.model_loader import (
    RegistryModelSettings,
    ResolvedModel,
    load_reranker_with_lineage,
)
from src.serving.schemas import RerankedVideo
from src.pipeline.simulate_policy_round import _to_candidate_videos, build_pool_feature_frame

logger = logging.getLogger(__name__)
JOB_NAME: Final = "daily_recommendations"
_REVISION: Final = os.getenv("AUTORESEARCH_REVISION", "unknown")

_DURATION_PATTERN = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

RECOMMENDATIONS_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("dt", "DATE"),
    bigquery.SchemaField("user_id", "STRING"),
    bigquery.SchemaField("video_id", "STRING"),
    bigquery.SchemaField("rank", "INTEGER"),
    bigquery.SchemaField("ctr_score", "FLOAT"),
    bigquery.SchemaField("model_run_id", "STRING"),
    bigquery.SchemaField("model_version", "STRING"),
    bigquery.SchemaField("events_dt", "DATE"),
    bigquery.SchemaField("generated_at", "TIMESTAMP"),
]


def parse_iso8601_duration(value: object) -> int:
    """ISO8601 duration 문자열(PT4M29S)을 초로 변환한다. 해석 불가 값은 0."""
    if not isinstance(value, str):
        return 0
    match = _DURATION_PATTERN.fullmatch(value)
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds or 0)


def to_recommendation_rows(
    user_id: str,
    ranked: list[RerankedVideo],
    *,
    dt: date,
    events_dt: date,
    model_run_id: str,
    model_version: str | None,
    generated_at: datetime,
) -> list[dict]:
    """채점 결과를 결정론적 순위 행으로 조립한다(동점은 video_id 오름차순)."""
    ordered = sorted(ranked, key=lambda item: (-item.ctr_score, item.video_id))
    return [
        {
            "dt": dt,
            "user_id": user_id,
            "video_id": item.video_id,
            "rank": position,
            "ctr_score": float(item.ctr_score),
            "model_run_id": model_run_id,
            "model_version": model_version,
            "events_dt": events_dt,
            "generated_at": generated_at,
        }
        for position, item in enumerate(ordered, start=1)
    ]


def ensure_output_table(client: bigquery.Client, table_id: str) -> None:
    """출력 테이블이 없으면 dt DAY 파티션 스키마로 생성한다(있으면 무시)."""
    table = bigquery.Table(table_id, schema=RECOMMENDATIONS_SCHEMA)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="dt"
    )
    client.create_table(table, exists_ok=True)


def write_partition(
    client: bigquery.Client, table_id: str, frame: pd.DataFrame, dt: date
) -> None:
    """해당 날짜 파티션을 원자적으로 대체한다(재실행 멱등)."""
    destination = f"{table_id}${dt.strftime('%Y%m%d')}"
    job_config = bigquery.LoadJobConfig(
        schema=RECOMMENDATIONS_SCHEMA,
        write_disposition="WRITE_TRUNCATE",
    )
    client.load_table_from_dataframe(frame, destination, job_config=job_config).result()
```

주의: `_to_candidate_videos`는 `simulate_policy_round`의 기존 헬퍼를 재사용한다(피처 프레임 → CandidateVideo 변환, NaN 정규화 포함). private 이름이지만 동일 패키지 내 소비이며, 공용화 리팩터링은 이 계획의 범위 밖이다.

- [ ] **Step 4: 통과 확인**

Run: `uv run python -m pytest tests/test_daily_recommendations.py -v`
Expected: PASS (4건)

- [ ] **Step 5: 커밋**

```bash
git add src/pipeline/daily_recommendations.py tests/test_daily_recommendations.py
git commit -m "feat: 일일 추천 순위 조립·멱등 적재 헬퍼 추가 (#216)

동점 video_id tie-break 결정론, dt 파티션 데코레이터 + WRITE_TRUNCATE
멱등 적재를 fake client 테스트로 고정."
```

---

### Task 4: 배치 main·CLI + db-dtypes 의존성 (`daily_recommendations.py` 2/2)

**Files:**
- Modify: `src/pipeline/daily_recommendations.py` (main/로더/CLI 추가)
- Modify: `pyproject.toml`, `uv.lock` (db-dtypes)
- Test: `tests/test_daily_recommendations.py` (테스트 추가)

**Interfaces:**
- Consumes: Task 1 `load_reranker_with_lineage`/`RegistryModelSettings`/`ResolvedModel`, Task 2 `to_personas_frame`, Task 3 헬퍼 전부, 기존 `derive_wide_events`/`load_events_from_bigquery`/`build_pool_feature_frame`/`_to_candidate_videos`
- Produces:
  - `run_batch(*, candidate_dt=None, events_dt=None, max_users=None, output_table=None, dry_run=False, max_skip_ratio=0.1, bq_client=None, resolved=None, videos_raw=None, personas=None, events=None, clock=utc_now) -> dict[str, object]` — 테스트 주입 가능한 배치 본체. 반환 키: `dt, events_dt, users, skipped_users, rows, model_run_id, model_version, dry_run`
  - `main(argv: Sequence[str] | None = None) -> int` — 공개 CLI 경계. 성공 시 spec 필드와 `event/job/status/contract_version`을 합친 `job_summary` 한 줄 출력.
  - CLI: `python -m src.pipeline.daily_recommendations --candidate-dt ... --events-dt ... --max-users ... --output-table ... --dry-run --max-skip-ratio ...`

- [ ] **Step 1: 실패하는 테스트 추가** — `tests/test_daily_recommendations.py` 말미에

```python
import json

import numpy as np
import pytest

import src.pipeline.daily_recommendations as daily
from src.pipeline.daily_recommendations import run_batch
from src.serving.model_loader import ResolvedModel
from src.serving.service import Reranker


class _EverythingHalfModel:
    """모든 후보에 0.5를 주는 stub — 채점 경로만 검증한다."""

    def predict_proba(self, features):
        return np.column_stack([np.full(len(features), 0.5), np.full(len(features), 0.5)])


def _stub_resolved() -> ResolvedModel:
    feature_columns = (
        "age_group", "occupation", "historical_category_affinity",
        "recent_click_count_7d", "recent_watch_time_7d", "recent_like_count_7d",
        "category_id", "duration_sec", "view_count", "like_ratio",
        "comment_ratio", "days_since_upload", "historical_category_match",
        "preferred_category_match", "topic_similarity",
    )
    reranker = Reranker(
        model=_EverythingHalfModel(),
        feature_columns=feature_columns,
        categorical_categories={},
    )
    return ResolvedModel(reranker=reranker, run_id="run-e2e", model_version="9")


def _videos_raw(n: int = 5) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "video_id": [f"v{i}" for i in range(n)],
            "categoryId": ["Gaming"] * n,
            "duration": [100 + i for i in range(n)],
            "viewCount": [1000] * n,
            "likeCount": [10] * n,
            "commentCount": [1] * n,
            "publishedAt": ["2026-07-01"] * n,
        }
    )


def _personas(users: list[str]) -> pd.DataFrame:
    return pd.DataFrame(
        {
            "uuid": users,
            "age": [25] * len(users),
            "occupation": ["student"] * len(users),
            "hobbies_and_interests_list": ['["게임"]'] * len(users),
            "hobbies_and_interests": ["게임"] * len(users),
        }
    )


def _empty_events() -> pd.DataFrame:
    frame = pd.DataFrame(
        columns=["event_id", "user_id", "video_id", "timestamp", "clicked", "liked", "watch_time_sec"]
    )
    return frame.astype(
        {"event_id": "string", "user_id": "string", "video_id": "string",
         "timestamp": "string", "clicked": "Int64", "liked": "Int64", "watch_time_sec": "Int64"}
    )


def test_run_batch_scores_all_users_and_writes_one_partition():
    client = _FakeClient()
    report = run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        bq_client=client,
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1", "u2"]),
        events=_empty_events(),
    )
    assert (report["users"], report["skipped_users"], report["rows"]) == (2, 0, 10)
    assert report["model_run_id"] == "run-e2e" and report["model_version"] == "9"
    (frame, destination, cfg), = client.loads
    assert destination.endswith("$20260721")
    assert cfg.write_disposition == "WRITE_TRUNCATE"
    assert len(frame) == 10  # 유저 2 × 후보 5
    assert set(frame["rank"]) == {1, 2, 3, 4, 5}


def test_run_batch_dry_run_writes_nothing():
    client = _FakeClient()
    report = run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 21),
        dry_run=True,
        bq_client=client,
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1"]),
        events=_empty_events(),
    )
    assert report["dry_run"] is True and report["rows"] == 5
    assert client.loads == []


def test_run_batch_rejects_empty_candidate_partition():
    client = _FakeClient()
    with pytest.raises(RuntimeError, match="No candidates"):
        run_batch(
            candidate_dt=date(2026, 7, 21),
            events_dt=date(2026, 7, 21),
            bq_client=client,
            resolved=_stub_resolved(),
            videos_raw=pd.DataFrame(),
            personas=_personas(["u1"]),
            events=_empty_events(),
        )
    assert client.loads == []


def test_run_batch_loads_exactly_one_events_partition(monkeypatch):
    calls = []

    def _load_events(start_date, end_date):
        calls.append((start_date, end_date))
        return pd.DataFrame()

    monkeypatch.setattr(daily, "load_events_from_bigquery", _load_events)
    monkeypatch.setattr(daily, "derive_wide_events", lambda frame: _empty_events())
    run_batch(
        candidate_dt=date(2026, 7, 21),
        events_dt=date(2026, 7, 20),
        dry_run=True,
        bq_client=_FakeClient(),
        resolved=_stub_resolved(),
        videos_raw=_videos_raw(),
        personas=_personas(["u1"]),
    )
    assert calls == [("2026-07-20", "2026-07-20")]


def test_run_batch_fails_without_write_when_skip_ratio_exceeded(monkeypatch):
    # u2의 조립 실패를 유저 단위로 격리하되 1/2 > 0.4이면 전체 적재를 중단한다.
    client = _FakeClient()
    original = daily.build_pool_feature_frame

    def _build_or_fail(*, user_id, **kwargs):
        if user_id == "u2":
            raise KeyError("broken persona")
        return original(user_id=user_id, **kwargs)

    monkeypatch.setattr(daily, "build_pool_feature_frame", _build_or_fail)

    with pytest.raises(RuntimeError):
        run_batch(
            candidate_dt=date(2026, 7, 21),
            events_dt=date(2026, 7, 21),
            max_skip_ratio=0.4,
            bq_client=client,
            resolved=_stub_resolved(),
            videos_raw=_videos_raw(),
            personas=_personas(["u1", "u2"]),
            events=_empty_events(),
        )
    assert client.loads == []


def test_main_emits_public_job_summary(monkeypatch, capsys):
    monkeypatch.setattr(
        daily,
        "run_batch",
        lambda **kwargs: {
            "event": "job_summary",
            "contract_version": "batch-contract-v1",
            "job": "daily_recommendations",
            "status": "succeeded",
            "dt": "2026-07-21",
            "events_dt": "2026-07-20",
            "users": 2,
            "skipped_users": 0,
            "rows": 10,
            "model_run_id": "run-e2e",
            "model_version": "9",
            "dry_run": True,
        },
    )
    assert daily.main(["--candidate-dt", "2026-07-21", "--dry-run"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert (payload["event"], payload["status"], payload["rows"]) == (
        "job_summary",
        "succeeded",
        10,
    )


def test_main_rejects_invalid_ratio_with_exit_2(capsys):
    assert daily.main(["--max-skip-ratio", "1.1"]) == 2
    assert json.loads(capsys.readouterr().out)["error_type"] == "invalid_arguments"
```

- [ ] **Step 2: 실패 확인**

Run: `uv run python -m pytest tests/test_daily_recommendations.py -v`
Expected: 신규 7건 FAIL — `ImportError: cannot import name 'run_batch'`

- [ ] **Step 3: 구현** — `daily_recommendations.py`에 추가

```python
def _required_tracking_uri() -> str:
    """registry 로드에 필요한 MLFLOW_TRACKING_URI를 읽는다."""
    import os

    value = os.getenv("MLFLOW_TRACKING_URI")
    if value is None or not value.strip():
        raise RuntimeError("MLFLOW_TRACKING_URI is required to load the champion model.")
    return value


def _default_registry_settings() -> RegistryModelSettings:
    """배치 기본 모델 설정 — models:/ctr-model@champion (env로 재정의 가능)."""
    import os

    return RegistryModelSettings(
        tracking_uri=_required_tracking_uri(),
        model_name=os.getenv("RERANK_REGISTRY_MODEL_NAME", "ctr-model"),
        alias=os.getenv("RERANK_REGISTRY_ALIAS", "champion"),
    )


def _max_partition_date(client: bigquery.Client, table_id: str) -> date:
    """테이블의 MAX(dt) 파티션 날짜를 조회한다."""
    query = f"SELECT MAX(dt) AS max_dt FROM `{table_id}`"
    row = next(iter(client.query(query).result()))
    if row.max_dt is None:
        raise RuntimeError(f"No partitions found in {table_id}")
    return row.max_dt


def _load_candidates(client: bigquery.Client, table_id: str, dt: date) -> pd.DataFrame:
    """후보 파티션을 학습 계약 컬럼명으로 조회하고 duration을 초로 정규화한다."""
    query = f"""
    SELECT video_id,
           video_category AS categoryId,
           video_duration AS duration,
           video_view_count AS viewCount,
           video_like_count AS likeCount,
           video_comment_count AS commentCount,
           video_published_at AS publishedAt
    FROM `{table_id}`
    WHERE dt = '{dt.isoformat()}'
    """
    frame = client.query(query).to_dataframe()
    frame["duration"] = frame["duration"].apply(parse_iso8601_duration)
    return frame


def _load_virtual_users(client: bigquery.Client, table_id: str) -> pd.DataFrame:
    """가상 유저 전원의 어댑터 입력 컬럼을 조회한다."""
    query = f"""
    SELECT user_id, age, occupation, hobby_keywords, interest_keywords, lifestyle_keywords
    FROM `{table_id}`
    """
    return client.query(query).to_dataframe()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_batch(
    *,
    candidate_dt: date | None = None,
    events_dt: date | None = None,
    max_users: int | None = None,
    output_table: str | None = None,
    dry_run: bool = False,
    max_skip_ratio: float = 0.1,
    bq_client: bigquery.Client | None = None,
    resolved: ResolvedModel | None = None,
    videos_raw: pd.DataFrame | None = None,
    personas: pd.DataFrame | None = None,
    events: pd.DataFrame | None = None,
    clock: Callable[[], datetime] = _utc_now,
) -> dict[str, object]:
    """일일 추천 배치를 실행하고 요약 리포트를 반환한다.

    bq_client·resolved·videos_raw·personas·events·clock은 테스트 주입용이며,
    None이면 실환경(BigQuery·MLflow registry)에서 로드한다.
    """
    import os

    dataset = f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}"
    trending_table = f"{dataset}." + os.getenv(
        "CTR_TRAINING_BQ_VIDEOS_TABLE", "data_lake_youtube_trending_kr"
    )
    users_table = f"{dataset}." + os.getenv(
        "CTR_TRAINING_BQ_VIRTUAL_USERS_TABLE", "asset_virtual_user_vu_1000"
    )
    output_table_id = f"{dataset}." + (
        output_table
        or os.getenv("CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE", "user_recommendations")
    )

    if resolved is None:
        resolved = load_reranker_with_lineage(_default_registry_settings())  # fail-fast
    reranker = resolved.reranker
    if bq_client is None:
        bq_client = bigquery.Client(project=BIGQUERY_PROJECT)

    if candidate_dt is None:
        candidate_dt = _max_partition_date(bq_client, trending_table)
    if events_dt is None:
        events_dt = _max_partition_date(
            bq_client,
            f"{dataset}." + os.getenv("CTR_TRAINING_BQ_ACTION_LOG_TABLE", "data_lake_action_log"),
        )

    if videos_raw is None:
        videos_raw = _load_candidates(bq_client, trending_table, candidate_dt)
    if videos_raw.empty:
        raise RuntimeError(f"No candidates in partition dt={candidate_dt}")
    if personas is None:
        personas = to_personas_frame(_load_virtual_users(bq_client, users_table))
    if personas.empty:
        raise RuntimeError("No virtual users available for scoring")
    if events is None:
        # 단일 파티션 소비 계약: 파티션 간 UNION은 attribution·집계를 오염시킨다.
        iso = events_dt.isoformat()
        events = derive_wide_events(load_events_from_bigquery(iso, iso))

    user_ids = personas["uuid"].astype(str).tolist()
    if max_users is not None:
        user_ids = user_ids[:max_users]

    # events_dt 파티션 전체를 과거 이력으로 포함하되 이후 이벤트는 보지 않는다.
    as_of = (events_dt + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")

    generated_at = clock()
    all_rows: list[dict] = []
    skipped: list[str] = []
    for user_id in user_ids:
        try:
            frame = build_pool_feature_frame(
                personas=personas,
                events=events,
                videos_raw=videos_raw,
                user_id=user_id,
                as_of=as_of,
            )
            ranked = reranker.rerank(_to_candidate_videos(frame, reranker.feature_columns))
            all_rows.extend(
                to_recommendation_rows(
                    user_id,
                    ranked,
                    dt=candidate_dt,
                    events_dt=events_dt,
                    model_run_id=resolved.run_id,
                    model_version=resolved.model_version,
                    generated_at=generated_at,
                )
            )
        except Exception as error:  # noqa: BLE001 - spec가 유저 단위 격리를 요구하는 경계
            logger.warning(
                "daily recommendation user quarantined",
                extra={"user_id": user_id, "exception_type": type(error).__name__},
            )
            skipped.append(user_id)

    if user_ids and len(skipped) / len(user_ids) > max_skip_ratio:
        raise RuntimeError(
            f"Skip ratio {len(skipped)}/{len(user_ids)} exceeded {max_skip_ratio}; aborting without write."
        )

    if not dry_run:
        ensure_output_table(bq_client, output_table_id)
        output_frame = pd.DataFrame(
            all_rows,
            columns=[field.name for field in RECOMMENDATIONS_SCHEMA],
        )
        write_partition(bq_client, output_table_id, output_frame, candidate_dt)

    report: dict[str, object] = {
        "event": "job_summary",
        "contract_version": BATCH_CONTRACT_VERSION,
        "job": JOB_NAME,
        "status": "succeeded",
        "dt": candidate_dt.isoformat(),
        "partition_date": candidate_dt.isoformat(),
        "events_dt": events_dt.isoformat(),
        "users": len(user_ids),
        "skipped_users": len(skipped),
        "rows": len(all_rows),
        "model_run_id": resolved.run_id,
        "model_version": resolved.model_version,
        "dry_run": dry_run,
    }
    return report


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD") from error


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _skip_ratio(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="일일 추천 결과 BQ 적재 배치")
    parser.add_argument(
        "--version",
        action="version",
        version=json.dumps(
            {
                "application_revision": _REVISION,
                "contract_version": BATCH_CONTRACT_VERSION,
            },
            sort_keys=True,
        ),
    )
    parser.add_argument("--candidate-dt", type=_iso_date)
    parser.add_argument("--events-dt", type=_iso_date)
    parser.add_argument("--max-users", type=_positive_int)
    parser.add_argument("--output-table")
    parser.add_argument("--max-skip-ratio", type=_skip_ratio, default=0.1)
    parser.add_argument("--dry-run", action="store_true")
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), flush=True)


def _failure_summary(error_type: str) -> dict[str, object]:
    return {
        "event": "job_summary",
        "contract_version": BATCH_CONTRACT_VERSION,
        "job": JOB_NAME,
        "status": "failed",
        "error_type": error_type,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 인자를 검증·실행하고 공개 종료 코드를 반환한다."""
    try:
        args = _build_parser().parse_args(argv)
    except BatchArgumentError:
        _emit(_failure_summary("invalid_arguments"))
        return 2

    try:
        report = run_batch(
            candidate_dt=args.candidate_dt,
            events_dt=args.events_dt,
            max_users=args.max_users,
            output_table=args.output_table,
            dry_run=args.dry_run,
            max_skip_ratio=args.max_skip_ratio,
        )
    except Exception as error:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("daily_recommendations failed (%s)", type(error).__name__)
        _emit(_failure_summary("runtime_failure"))
        return 1

    _emit(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
```

- [ ] **Step 4: db-dtypes 의존성 추가** — BQ `to_dataframe()`의 잠재 미선언 의존성(2026-07-21 실측)

`pyproject.toml`의 `[project] dependencies`(`google-cloud-bigquery>=3.20`가 있는 목록)에 한 줄 추가:

```toml
    "db-dtypes>=1.2",
```

Run: `uv lock && uv sync`
Expected: lock 갱신 성공, 이후 `uv run python -c "import db_dtypes"` 통과

- [ ] **Step 5: 통과 확인**

Run: `uv run python -m pytest tests/test_daily_recommendations.py -v`
Expected: 전부 PASS (11건)

- [ ] **Step 6: 커밋**

```bash
git add src/pipeline/daily_recommendations.py tests/test_daily_recommendations.py pyproject.toml uv.lock
git commit -m "feat: 일일 추천 배치 main·CLI 및 db-dtypes 의존성 추가 (#216)

champion 모델 로드(fail-fast) -> 단일 파티션 재료 -> 유저 격리 채점
(skip 비율 임계) -> dt 파티션 멱등 적재. 테스트는 전부 주입 기반."
```

---

### Task 5: 공개 배치 이미지·계약 등재 + 전체 검증

**Files:**
- Modify: `docs/specs/2026-07-13-public-batch-execution-contract.md`
- Modify: `Dockerfile.app`
- Modify: `.github/workflows/ci.yml`
- Modify: `.github/workflows/release.yml`
- Modify: `tests/test_release_workflow.py`
- Test: `tests/test_release_workflow.py`, 전체 스위트

**Interfaces:**
- Consumes: Task 4의 CLI 계약

- [ ] **Step 1: 실패하는 이미지 공개 명령 테스트 작성** — `tests/test_release_workflow.py`

기존 `test_release_workflow_verifies_all_public_batch_commands`의 module 목록에 새 명령을 추가하고, application image가 `src/`를 포함하는지 고정한다.

```python
def test_release_workflow_verifies_all_public_batch_commands():
    workflow_text = WORKFLOW_PATH.read_text(encoding="utf-8")

    for module in (
        "autoresearch.jobs.youtube_trending",
        "autoresearch.jobs.action_log",
        "autoresearch.jobs.action_log_quality",
        "src.pipeline.daily_recommendations",
    ):
        assert module in workflow_text
    assert "org.opencontainers.image.revision" in workflow_text
    assert ".application_revision" in workflow_text
    assert ".contract_version" in workflow_text


def test_application_image_contains_daily_recommendations_command():
    dockerfile = APPLICATION_DOCKERFILE.read_text(encoding="utf-8")
    assert "COPY src ./src" in dockerfile
```

Run: `uv run python -m pytest tests/test_release_workflow.py -v`
Expected: FAIL — release workflow와 `Dockerfile.app`에 새 공개 명령이 아직 없음.

- [ ] **Step 2: application image와 CI/release smoke 경로 갱신**

`Dockerfile.app`의 runtime copy 구간:

```dockerfile
COPY autoresearch ./autoresearch
COPY src ./src
```

`.github/workflows/ci.yml`의 application image smoke step에 추가:

```yaml
docker run --rm autoresearch:ci \
  python -m src.pipeline.daily_recommendations --help
docker run --rm autoresearch:ci \
  python -m src.pipeline.daily_recommendations --version
```

`.github/workflows/release.yml`의 digest 검증 module 목록에 추가:

```bash
src.pipeline.daily_recommendations
```

Run: `uv run python -m pytest tests/test_release_workflow.py -v`
Expected: PASS.

- [ ] **Step 3: 계약 문서 등재**

`docs/specs/2026-07-13-public-batch-execution-contract.md`를 열어 기존 명령 등재 형식(섹션/표 스타일)을 확인하고, 같은 형식으로 다음 내용을 추가한다:

> **`python -m src.pipeline.daily_recommendations`** — champion 모델로 일일 트렌딩 후보를 가상 유저 전원에 대해 채점해 `user_recommendations` dt 파티션에 멱등 적재.
> 인자: `--candidate-dt`(기본 후보 MAX(dt)), `--events-dt`(기본 action log MAX(dt), **단일 파티션만 소비**), `--max-users`, `--output-table`(기본 `user_recommendations`), `--max-skip-ratio`(기본 0.1), `--dry-run`.
> 환경변수: `MLFLOW_TRACKING_URI`(필수), `RERANK_REGISTRY_MODEL_NAME`(기본 `ctr-model`), `RERANK_REGISTRY_ALIAS`(기본 `champion`), `CTR_TRAINING_BQ_*`(기존 체계), `CTR_TRAINING_BQ_VIRTUAL_USERS_TABLE`(기본 `asset_virtual_user_vu_1000`), `CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE`(기본 `user_recommendations`).
> 출력: 정상 종료 시 마지막 stdout event는 `event=job_summary`, `job=daily_recommendations`, `status=succeeded`이며 `users`, `skipped_users`, `rows`, `model_run_id`, `model_version`, `events_dt`, `dry_run`을 포함한다. 인자 오류는 exit 2, registry/BQ/후보 0건/skip 임계 초과는 exit 1이며 실패 summary를 남긴다.
> 격리 진단: 이 spec의 user quarantine 요구를 위해 stderr warning에 `user_id`와 예외 타입만 기록한다. stdout summary에는 user 식별자를 넣지 않으며 persona·원문 예외 메시지는 기록하지 않는다. 공통 식별자 로그 금지 규칙의 범위가 stdout telemetry임을 이 명령 섹션에서 명시한다.
> 스케줄·재시도·타임아웃은 `Autoresearch-airflow` 소유.

- [ ] **Step 4: 전체 스위트 + 이미지·문서 검증**

Run: `uv run python -m pytest -q` → 전부 PASS
Run: `git diff --check` → 공백 오류 없음
Run: `docker build -f Dockerfile.app -t autoresearch:daily-recommendations .` → exit 0
Run: `docker run --rm autoresearch:daily-recommendations python -m src.pipeline.daily_recommendations --help` → exit 0
Run: `docker run --rm autoresearch:daily-recommendations python -m src.pipeline.daily_recommendations --version` → `application_revision`, `batch-contract-v1` JSON 출력

- [ ] **Step 5: 커밋 + push**

```bash
git add Dockerfile.app .github/workflows/ci.yml .github/workflows/release.yml \
  tests/test_release_workflow.py docs/specs/2026-07-13-public-batch-execution-contract.md
git commit -m "feat: 공개 이미지에 일일 추천 배치 명령 등재 (#216)"
git push -u origin feat/216-daily-recommendations-batch
```

---

## Self-Review 결과

- **Spec coverage:** registry 소스+`load_reranker_with_lineage`(Task 1), 어댑터 정식화·v2 회귀 테스트(Task 2), 순위 결정론·9컬럼 스키마·파티션 멱등(Task 3), `build_pool_feature_frame` 재사용·단일 events 파티션·유저 격리·skip 임계·fail-fast·dry-run·CLI·db-dtypes(Task 4), 공개 계약·application image·CI/release smoke(Task 5). 콜드스타트 정상 경로는 Task 4 e2e(`_empty_events`)가 커버하고, 후보 0건과 인자 오류도 각각 테스트한다.
- **Type consistency:** `ResolvedModel(reranker, run_id: str, model_version: str | None)`을 Task 1 정의 → Task 4 소비 일치. `to_recommendation_rows` 키워드 인자와 테스트 호출 일치. `RECOMMENDATIONS_SCHEMA` 9컬럼 = spec 테이블 계약 9컬럼.
- **Placeholder scan:** 미결정 표식이나 모호한 후속 지시가 없다. 각 변경 단계에 대상 파일, 코드, 실행 명령, 기대 결과가 있다.
- **알려진 리스크 (실행자 주의):**
  - `_to_candidate_videos` 재사용은 simulate_policy_round의 private 헬퍼 소비다 — 리뷰에서 공용화 요구가 나오면 별도 후속(구조 변경)으로 미룬다.
  - spec 충실성을 위해 기존 `build_pool_feature_frame`을 유저마다 호출하므로 fake e2e는 6,983명 성능을 증명하지 않는다. 배포 전 운영 자격증명 환경에서 먼저 `--max-users 10 --dry-run`, 다음 전체 `--dry-run`으로 소요 시간과 메모리를 기록한다. 목표 수 분을 넘으면 동작 변경 없이 공용 조립 함수 내부의 video/offline 계산을 precompute하는 후속 리팩터링을 별도 이슈로 분리한다.
  - spec의 user quarantine 요구와 공통 stdout 식별자 금지 계약을 함께 지키기 위해 raw `user_id`는 stderr warning에만 두고 stdout `job_summary`에는 count만 둔다. Task 5에서 이 경계를 공개 계약에 명시해 운영자가 두 채널을 혼동하지 않게 한다.
