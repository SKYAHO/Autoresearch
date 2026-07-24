# promote-model CLI Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `src.cli`에 `promote-model` 서브커맨드를 신설해, Airflow `ctr_model_promote`
DAG(Autoresearch-airflow#137)가 사람 개입 없이 champion alias를 조건부로
승격할 수 있게 한다.

**Architecture:** 게이트 판정 로직은 신규 모듈 `src/tracking/promote.py`에 두고,
`src/cli.py`는 다른 서브커맨드와 동일하게 얇게 그 `main()`을 호출·exit code로
변환만 한다. 기존 `src/tracking/registry.py`의 `get_latest_version`,
`get_model_versions`, `get_model_metrics_by_alias`, `set_model_alias`를 재사용하고,
downsampling 페어링 존재 확인만 `MlflowClient.search_model_versions`로 경량
재구현한다.

**Tech Stack:** Python, typer(CLI), mlflow(`MlflowClient`), pytest + `unittest.mock`.

## Global Constraints

- CLI 계약(옵션 이름·기본값·환경변수·종료 코드)은 이슈 #342 / spec
  `docs/specs/2026-07-25-promote-model-champion-gate.md`을 그대로 따른다 — 임의 변경 금지.
- `MLFLOW_TRACKING_URI`는 CLI 인자가 아니라 환경변수로 읽는다.
- 종료 코드는 성공/no-op이 `0`, 그 외 전부 `0`이 아닌 값이면 되지만, stderr 메시지는
  `[게이트 미달]`과 `[에러]`를 반드시 구분한다.
- 새 모듈(`src/tracking/promote.py`)은 최상단에 Module Responsibility 형식
  docstring을 갖는다(`.claude/docs/agent-python-reference.md`).
- 모든 함수는 반환 타입을 포함한 타입 힌트를 갖는다.
- 커밋 메시지는 `<type>: <한국어 설명>` 형식, 하나의 논리적 변경만 담는다.
- 테스트는 `tests/test_<module>.py`에 배치하고 실제 MLflow 서버 없이 fake
  client로 격리한다(`tests/test_serving_model_registry.py`의 `_PairingClient`
  패턴을 참고).

---

## File Structure

- **Create:** `src/tracking/promote.py` — 게이트 판정 로직(`GateRejectedError`, `main()`).
- **Modify:** `src/cli.py` — `promote_model` typer 커맨드 추가(파일 끝 `if __name__` 앞).
- **Create:** `tests/test_tracking_promote.py` — `promote.main()` 단위 테스트, fake
  `MlflowClient`(`_PromoteClient`) 포함.
- **Modify:** `tests/test_cli.py` — `promote_model` 커맨드의 exit code/메시지 테스트 추가.

---

### Task 1: `promote.py` 골격 + no-op 케이스

**Files:**
- Create: `src/tracking/promote.py`
- Create: `tests/test_tracking_promote.py`

**Interfaces:**
- Produces: `promote.GateRejectedError(RuntimeError)`,
  `promote.main(model_name: str, champion_alias: str, calibration_model_name: str) -> str | None`
  (반환값: 승격된 버전 문자열, 또는 후보 없음이면 `None`).
- Consumes: `src.tracking.registry.get_latest_version(model_name: str) -> Optional[str]`,
  `src.tracking.registry.get_model_versions(model_name: str) -> list[Dict]`
  (각 dict는 `"version"`, `"aliases"`, `"run_id"`, `"creation_timestamp"` 키를 가짐).

- [ ] **Step 1: 테스트 파일과 fake client를 작성한다**

`tests/test_tracking_promote.py` 새 파일:

```python
from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from src.tracking import promote, registry  # noqa: E402

MODEL_NAME = "ctr-model"
CALIBRATION_MODEL_NAME = "ctr-calibration-model"


def _version(version, *, aliases=None, run_id=None, tags=None):
    return SimpleNamespace(
        version=version,
        aliases=aliases or [],
        run_id=run_id or f"run-v{version}",
        tags=tags or {},
        creation_timestamp=0,
    )


class _PromoteClient:
    """model_name(main)과 calibration_model_name을 name으로 구분하는 가짜 client.

    tests/test_serving_model_registry.py의 _PairingClient와 같은 패턴 —
    실제 MLflow 서버 없이 registry.py/promote.py가 호출하는 MlflowClient
    메서드 표면만 흉내낸다.
    """

    def __init__(self, *, main_versions=None, calibration_versions=None, runs=None):
        self.main_versions = main_versions or []
        self.calibration_versions = calibration_versions or []
        self.runs = runs or {}
        self.set_alias_calls: list[tuple[str, str, str]] = []

    def _versions_for(self, name):
        return self.main_versions if name == MODEL_NAME else self.calibration_versions

    def search_model_versions(self, filter_string):
        name = MODEL_NAME if MODEL_NAME in filter_string else CALIBRATION_MODEL_NAME
        return self._versions_for(name)

    def get_model_version(self, name, version):
        for v in self._versions_for(name):
            if v.version == str(version):
                return v
        raise registry.MlflowException(f"version not found: {name} v{version}")

    def get_model_version_by_alias(self, name, alias):
        for v in self._versions_for(name):
            if alias in v.aliases:
                return v
        raise registry.MlflowException(f"Registered model alias {alias} not found")

    def get_run(self, run_id):
        return SimpleNamespace(data=SimpleNamespace(metrics=self.runs.get(run_id, {})))

    def set_registered_model_alias(self, name, alias, version):
        self.set_alias_calls.append((name, alias, str(version)))


def _patch_client(monkeypatch, client):
    monkeypatch.setattr(registry, "MlflowClient", lambda: client)
    monkeypatch.setattr(promote, "MlflowClient", lambda: client)


def test_main_returns_none_when_no_versions_registered(monkeypatch):
    client = _PromoteClient(main_versions=[])
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)

    assert result is None
    assert client.set_alias_calls == []


def test_main_returns_none_when_latest_is_already_champion(monkeypatch):
    v5 = _version("5", aliases=["champion"], run_id="run-5")
    client = _PromoteClient(main_versions=[v5], runs={"run-5": {"val_roc_auc": 0.80}})
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)

    assert result is None
    assert client.set_alias_calls == []
```

- [ ] **Step 2: 테스트 실행 — import 실패로 fail하는지 확인**

Run: `uv run python -m pytest tests/test_tracking_promote.py -v`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.tracking.promote'`
(또는 `ImportError: cannot import name 'promote'`)

- [ ] **Step 3: `src/tracking/promote.py`를 작성한다 (no-op 케이스까지)**

```python
"""champion 승격 게이트 판정.

[파이프라인] 학습(src/pipeline/train.py) 이후, 서빙이 alias로 모델을 로드하기
전 — Model Registry의 champion alias를 신규 후보 버전으로 옮길지 판정하는
구간을 담당한다. Airflow ctr_model_promote DAG(Autoresearch-airflow#137)가
호출하는 promote-model CLI(src/cli.py)의 판정 본체다.

[기능] 최신 등록 버전을 후보로 삼아 held-out 지표(val_roc_auc)가 현재
champion 이상인지, downsampling 후보면 짝 calibration 버전이 등록돼 있는지
확인한 뒤 게이트를 통과하면 champion(+짝 calibration) alias를 옮긴다.

[비책임] 서빙 시점 alias resolve·페어링 검증(src/serving/model_loader.py의
_resolve_paired_calibration_run_id), Airflow DAG 스케줄링·재시도
(Autoresearch-airflow).
"""

from __future__ import annotations

from typing import Optional

from mlflow.tracking import MlflowClient

from src.tracking.registry import (
    get_latest_version,
    get_model_metrics_by_alias,
    get_model_versions,
    set_model_alias,
)


class GateRejectedError(RuntimeError):
    """게이트 조건(지표 비교 또는 downsampling 페어링) 미달로 승격이 거부됨."""


def main(
    model_name: str,
    champion_alias: str,
    calibration_model_name: str,
) -> Optional[str]:
    """게이트 통과 시 champion(+짝 calibration) alias를 최신 후보 버전으로 옮긴다.

    Args:
        model_name: main 모델 registry 이름.
        champion_alias: 승격 대상 alias(보통 'champion').
        calibration_model_name: 짝 calibration 모델 registry 이름.

    Returns:
        승격된 후보 버전 문자열. 평가할 신규 후보가 없으면(등록된 버전이
        없거나 최신 버전이 이미 champion) None.

    Raises:
        GateRejectedError: 게이트 조건 미달로 승격 거부.
        (기타) MLflow 연결 실패 등 실행 중 오류는 그대로 전파한다.
    """
    candidate_version = get_latest_version(model_name)
    if candidate_version is None:
        return None

    existing_versions = get_model_versions(model_name)
    champion_entry = next(
        (v for v in existing_versions if champion_alias in v["aliases"]), None
    )
    if champion_entry is not None and champion_entry["version"] == candidate_version:
        return None

    return candidate_version
```

이 시점에는 아직 게이트 판정을 하지 않고 항상 후보 버전을 반환한다 —
Step 4에서 두 테스트만 먼저 통과시키기 위한 최소 구현이다(Task 2에서
게이트 1을 채워 넣는다).

- [ ] **Step 4: 테스트 실행 — 두 케이스 통과 확인**

Run: `uv run python -m pytest tests/test_tracking_promote.py -v`
Expected: `2 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/tracking/promote.py tests/test_tracking_promote.py
git commit -m "feat: promote-model 게이트 모듈 골격 + no-op 판정 (#342)"
```

---

### Task 2: 게이트 1 — 지표 비교(부트스트랩·승격·거부·지표 누락)

**Files:**
- Modify: `src/tracking/promote.py`
- Modify: `tests/test_tracking_promote.py`

**Interfaces:**
- Consumes: `src.tracking.registry.get_model_metrics_by_alias(model_name: str, alias: str = "champion") -> Optional[Dict[str, float]]`.
- Produces: `promote.main`이 이제 게이트 1을 실제로 적용함(반환/예외 계약은 Task 1과 동일).

- [ ] **Step 1: 실패하는 테스트 4개를 추가한다**

`tests/test_tracking_promote.py` 끝에 추가:

```python
def test_main_promotes_when_no_champion_exists_bootstrap(monkeypatch):
    # champion alias가 아직 없으면 비교 대상이 없어 게이트 1을 자동 통과한다.
    v1 = _version("1", run_id="run-1")
    client = _PromoteClient(main_versions=[v1], runs={"run-1": {"val_roc_auc": 0.70}})
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)

    assert result == "1"
    assert client.set_alias_calls == [(MODEL_NAME, "champion", "1")]


def test_main_promotes_when_candidate_metric_is_better(monkeypatch):
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4")
    client = _PromoteClient(
        main_versions=[champion, candidate],
        runs={"run-3": {"val_roc_auc": 0.75}, "run-4": {"val_roc_auc": 0.80}},
    )
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)

    assert result == "4"
    assert client.set_alias_calls == [(MODEL_NAME, "champion", "4")]


def test_main_rejects_when_candidate_metric_is_worse(monkeypatch):
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4")
    client = _PromoteClient(
        main_versions=[champion, candidate],
        runs={"run-3": {"val_roc_auc": 0.80}, "run-4": {"val_roc_auc": 0.70}},
    )
    _patch_client(monkeypatch, client)

    with pytest.raises(promote.GateRejectedError, match="게이트1"):
        promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)
    assert client.set_alias_calls == []


def test_main_raises_plain_error_when_candidate_metric_missing(monkeypatch):
    # 게이트 미달(GateRejectedError)이 아니라 데이터 결함으로 다뤄야 한다 —
    # CLI 계약상 [게이트 미달]/[에러] 메시지가 갈려야 하므로 예외 타입으로 구분한다.
    candidate = _version("1", run_id="run-1")
    client = _PromoteClient(main_versions=[candidate], runs={"run-1": {}})
    _patch_client(monkeypatch, client)

    with pytest.raises(ValueError, match="val_roc_auc"):
        promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)
    assert client.set_alias_calls == []
```

- [ ] **Step 2: 테스트 실행 — 새 4개가 fail하는지 확인**

Run: `uv run python -m pytest tests/test_tracking_promote.py -v`
Expected: 기존 2개는 PASS, 새 4개는 FAIL(아직 게이트 1 미구현이므로 항상
승격되거나 `client.get_run`을 호출하지 않아 `AssertionError`/무비교로 실패).

- [ ] **Step 3: `promote.main`에 게이트 1을 구현한다**

`src/tracking/promote.py`의 `main()` 함수 전체(Task 1에서 작성한 버전)를
아래 완성본으로 교체한다:

```python
def main(
    model_name: str,
    champion_alias: str,
    calibration_model_name: str,
) -> Optional[str]:
    """게이트 통과 시 champion(+짝 calibration) alias를 최신 후보 버전으로 옮긴다.

    Args:
        model_name: main 모델 registry 이름.
        champion_alias: 승격 대상 alias(보통 'champion').
        calibration_model_name: 짝 calibration 모델 registry 이름.

    Returns:
        승격된 후보 버전 문자열. 평가할 신규 후보가 없으면(등록된 버전이
        없거나 최신 버전이 이미 champion) None.

    Raises:
        GateRejectedError: 게이트 조건 미달로 승격 거부.
        (기타) MLflow 연결 실패 등 실행 중 오류는 그대로 전파한다.
    """
    candidate_version = get_latest_version(model_name)
    if candidate_version is None:
        return None

    existing_versions = get_model_versions(model_name)
    champion_entry = next(
        (v for v in existing_versions if champion_alias in v["aliases"]), None
    )
    if champion_entry is not None and champion_entry["version"] == candidate_version:
        return None

    client = MlflowClient()
    candidate_run_id = _run_id_for_version(existing_versions, candidate_version)
    candidate_metrics = client.get_run(candidate_run_id).data.metrics
    candidate_val_roc_auc = candidate_metrics.get("val_roc_auc")
    if candidate_val_roc_auc is None:
        raise ValueError(
            f"{model_name} v{candidate_version}의 run({candidate_run_id})에 "
            "val_roc_auc 지표가 없습니다."
        )

    champion_metrics = get_model_metrics_by_alias(model_name, champion_alias)
    if champion_metrics is not None:
        champion_val_roc_auc = champion_metrics.get("val_roc_auc")
        if (
            champion_val_roc_auc is not None
            and candidate_val_roc_auc < champion_val_roc_auc
        ):
            raise GateRejectedError(
                f"게이트1 미달: 후보 {model_name} v{candidate_version} "
                f"val_roc_auc={candidate_val_roc_auc:.4f} < champion"
                f"({champion_alias}) val_roc_auc={champion_val_roc_auc:.4f}"
            )

    set_model_alias(model_name, champion_alias, candidate_version)
    return candidate_version
```

같은 파일에 헬퍼 함수를 `main()` 앞에 추가한다(`existing_versions`의
`"run_id"` 키를 재사용해 후보 버전의 run_id를 찾는다 — `get_model_versions`가
이미 `run_id`를 포함하므로 별도 client 호출 없이 얻을 수 있다):

```python
def _run_id_for_version(versions: list[dict], version: str) -> str:
    for entry in versions:
        if entry["version"] == version:
            return entry["run_id"]
    raise ValueError(f"버전 {version}의 run_id를 찾을 수 없습니다.")
```

그리고 파일 상단 import에 `MlflowClient`를 추가한다(이미 Task 1에서 추가돼
있다면 생략):

```python
from mlflow.tracking import MlflowClient
```

- [ ] **Step 4: 테스트 실행 — 6개 전부 통과 확인**

Run: `uv run python -m pytest tests/test_tracking_promote.py -v`
Expected: `6 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/tracking/promote.py tests/test_tracking_promote.py
git commit -m "feat: promote-model 게이트1(지표 비교) 구현 (#342)"
```

---

### Task 3: 게이트 2 — downsampling 페어링

**Files:**
- Modify: `src/tracking/promote.py`
- Modify: `tests/test_tracking_promote.py`

**Interfaces:**
- Produces: `promote._find_paired_calibration_version(client, calibration_model_name, main_run_id) -> Optional[str]`
  (모듈 내부 헬퍼, 테스트에서 `promote._find_paired_calibration_version`으로
  직접 호출하지 않고 `promote.main`을 통해서만 검증한다).

- [ ] **Step 1: 실패하는 테스트 2개를 추가한다**

`tests/test_tracking_promote.py` 끝에 추가:

```python
def test_main_rejects_downsampling_candidate_without_paired_calibration(monkeypatch):
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4", tags={"sampling_rate": "0.5"})
    client = _PromoteClient(
        main_versions=[champion, candidate],
        calibration_versions=[],
        runs={"run-3": {"val_roc_auc": 0.70}, "run-4": {"val_roc_auc": 0.80}},
    )
    _patch_client(monkeypatch, client)

    with pytest.raises(promote.GateRejectedError, match="게이트2"):
        promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)
    assert client.set_alias_calls == []


def test_main_promotes_downsampling_candidate_with_paired_calibration(monkeypatch):
    champion = _version("3", aliases=["champion"], run_id="run-3")
    candidate = _version("4", run_id="run-4", tags={"sampling_rate": "0.5"})
    cal_version = _version("2", run_id="run-cal-2", tags={"main_run_id": "run-4"})
    client = _PromoteClient(
        main_versions=[champion, candidate],
        calibration_versions=[cal_version],
        runs={"run-3": {"val_roc_auc": 0.70}, "run-4": {"val_roc_auc": 0.80}},
    )
    # set_model_alias의 기존 #300 순서 가드(CTR_SERVING_CALIBRATION_READY)를
    # 통과시켜야 우리 게이트2 이후의 실제 alias 이동까지 검증할 수 있다.
    monkeypatch.setenv("CTR_SERVING_CALIBRATION_READY", "true")
    _patch_client(monkeypatch, client)

    result = promote.main(MODEL_NAME, "champion", CALIBRATION_MODEL_NAME)

    assert result == "4"
    assert client.set_alias_calls == [
        (MODEL_NAME, "champion", "4"),
        (CALIBRATION_MODEL_NAME, "champion", "2"),
    ]
```

- [ ] **Step 2: 테스트 실행 — 새 2개가 fail하는지 확인**

Run: `uv run python -m pytest tests/test_tracking_promote.py -v`
Expected: 기존 6개 PASS, 새 2개 FAIL(게이트 2 미구현 — downsampling 후보가
페어링 확인 없이 그냥 승격되어 `set_alias_calls`가 기대와 다름).

- [ ] **Step 3: `promote.main`에 게이트 2를 구현한다**

`src/tracking/promote.py`의 `main()` 함수 전체(Task 2에서 작성한 버전)를
아래 완성본으로 교체한다(게이트 1 부분은 그대로이고, 게이트 1 통과 이후
게이트 2와 calibration alias 이동이 추가됐다):

```python
def main(
    model_name: str,
    champion_alias: str,
    calibration_model_name: str,
) -> Optional[str]:
    """게이트 통과 시 champion(+짝 calibration) alias를 최신 후보 버전으로 옮긴다.

    Args:
        model_name: main 모델 registry 이름.
        champion_alias: 승격 대상 alias(보통 'champion').
        calibration_model_name: 짝 calibration 모델 registry 이름.

    Returns:
        승격된 후보 버전 문자열. 평가할 신규 후보가 없으면(등록된 버전이
        없거나 최신 버전이 이미 champion) None.

    Raises:
        GateRejectedError: 게이트 조건 미달로 승격 거부.
        (기타) MLflow 연결 실패 등 실행 중 오류는 그대로 전파한다.
    """
    candidate_version = get_latest_version(model_name)
    if candidate_version is None:
        return None

    existing_versions = get_model_versions(model_name)
    champion_entry = next(
        (v for v in existing_versions if champion_alias in v["aliases"]), None
    )
    if champion_entry is not None and champion_entry["version"] == candidate_version:
        return None

    client = MlflowClient()
    candidate_run_id = _run_id_for_version(existing_versions, candidate_version)
    candidate_metrics = client.get_run(candidate_run_id).data.metrics
    candidate_val_roc_auc = candidate_metrics.get("val_roc_auc")
    if candidate_val_roc_auc is None:
        raise ValueError(
            f"{model_name} v{candidate_version}의 run({candidate_run_id})에 "
            "val_roc_auc 지표가 없습니다."
        )

    champion_metrics = get_model_metrics_by_alias(model_name, champion_alias)
    if champion_metrics is not None:
        champion_val_roc_auc = champion_metrics.get("val_roc_auc")
        if (
            champion_val_roc_auc is not None
            and candidate_val_roc_auc < champion_val_roc_auc
        ):
            raise GateRejectedError(
                f"게이트1 미달: 후보 {model_name} v{candidate_version} "
                f"val_roc_auc={candidate_val_roc_auc:.4f} < champion"
                f"({champion_alias}) val_roc_auc={champion_val_roc_auc:.4f}"
            )

    candidate_mv = client.get_model_version(name=model_name, version=candidate_version)
    sampling_rate = float((candidate_mv.tags or {}).get("sampling_rate", 1.0))
    calibration_version: Optional[str] = None
    if sampling_rate < 1.0:
        calibration_version = _find_paired_calibration_version(
            client, calibration_model_name, candidate_run_id
        )
        if calibration_version is None:
            raise GateRejectedError(
                f"게이트2 미달: 후보 {model_name} v{candidate_version}는 "
                f"downsampling(sampling_rate={sampling_rate})인데 "
                f"{calibration_model_name}에 main_run_id={candidate_run_id}와 "
                "짝지어진 버전이 없습니다."
            )

    set_model_alias(model_name, champion_alias, candidate_version)
    if calibration_version is not None:
        set_model_alias(calibration_model_name, champion_alias, calibration_version)
    return candidate_version
```

`get_model_versions`는 tag 정보를 포함하지 않으므로(버전, alias, run_id,
생성시각만 반환), sampling_rate tag는 `client.get_model_version`으로 별도
조회한다(Task 2에서 이미 채워진 `client = MlflowClient()` 변수를 재사용).
`main()` 앞에 헬퍼를 하나 추가한다:

```python
def _find_paired_calibration_version(
    client: MlflowClient, calibration_model_name: str, main_run_id: str
) -> Optional[str]:
    """calibration_model_name 버전 중 main_run_id tag가 일치하는 버전을 찾는다.

    model_loader._resolve_paired_calibration_run_id와 검증 불변식은 같지만
    조회 방향이 다르다(그쪽은 "이미 alias된 조합이 맞는가", 이쪽은 "alias
    걸기 전에 짝이 존재하는가") — 그래서 직접 재사용 대신 경량 재구현한다.
    """
    versions = client.search_model_versions(f"name='{calibration_model_name}'")
    matches = [v for v in versions if (v.tags or {}).get("main_run_id") == main_run_id]
    if not matches:
        return None
    return max(matches, key=lambda v: int(v.version)).version
```

- [ ] **Step 4: 테스트 실행 — 8개 전부 통과 확인**

Run: `uv run python -m pytest tests/test_tracking_promote.py -v`
Expected: `8 passed`

- [ ] **Step 5: 커밋**

```bash
git add src/tracking/promote.py tests/test_tracking_promote.py
git commit -m "feat: promote-model 게이트2(downsampling 페어링) 구현 (#342)"
```

---

### Task 4: CLI 어댑터 (`src/cli.py`)

**Files:**
- Modify: `src/cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `promote.main(model_name, champion_alias, calibration_model_name) -> Optional[str]`,
  `promote.GateRejectedError`.
- Produces: `cli.promote_model(model_name: str, champion_alias: str, calibration_model_name: str) -> None`
  (typer 커맨드, 커맨드 이름은 typer 기본 변환으로 `promote-model`).

- [ ] **Step 1: 실패하는 테스트 4개를 추가한다**

`tests/test_cli.py` 상단 import에 추가:

```python
import pytest
import typer
```

파일 끝에 추가:

```python
def test_promote_model_prints_ok_and_exits_zero_on_success(monkeypatch, capsys):
    monkeypatch.setattr(cli.promote, "main", lambda **kwargs: "4")

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
        calibration_model_name="ctr-calibration-model",
    )

    out = capsys.readouterr().out
    assert "[OK]" in out
    assert "v4" in out


def test_promote_model_prints_noop_message_when_no_candidate(monkeypatch, capsys):
    monkeypatch.setattr(cli.promote, "main", lambda **kwargs: None)

    cli.promote_model(
        model_name="ctr-model",
        champion_alias="champion",
        calibration_model_name="ctr-calibration-model",
    )

    out = capsys.readouterr().out
    assert "no-op" in out


def test_promote_model_exits_nonzero_with_gate_rejected_prefix(monkeypatch, capsys):
    def _raise(**kwargs):
        raise cli.promote.GateRejectedError("게이트1 미달: 예시 사유")

    monkeypatch.setattr(cli.promote, "main", _raise)

    with pytest.raises(typer.Exit) as exc_info:
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            calibration_model_name="ctr-calibration-model",
        )

    assert exc_info.value.exit_code == 1
    err = capsys.readouterr().err
    assert "[게이트 미달]" in err


def test_promote_model_exits_nonzero_with_error_prefix_on_unexpected_exception(
    monkeypatch, capsys
):
    def _raise(**kwargs):
        raise RuntimeError("connection refused")

    monkeypatch.setattr(cli.promote, "main", _raise)

    with pytest.raises(typer.Exit) as exc_info:
        cli.promote_model(
            model_name="ctr-model",
            champion_alias="champion",
            calibration_model_name="ctr-calibration-model",
        )

    assert exc_info.value.exit_code == 1
    err = capsys.readouterr().err
    assert "[에러]" in err
```

- [ ] **Step 2: 테스트 실행 — 새 4개가 fail하는지 확인**

Run: `uv run python -m pytest tests/test_cli.py -v`
Expected: 기존 테스트는 PASS, 새 4개는 FAIL —
`AttributeError: module 'src.cli' has no attribute 'promote'`.

- [ ] **Step 3: `src/cli.py`에 커맨드를 추가한다**

`src/cli.py`의 `from src.pipeline import ...` 줄 다음에 import를 추가한다:

```python
from src.tracking import promote  # noqa: E402
```

`run_pipeline` 함수와 `if __name__ == "__main__":` 사이에 새 커맨드를 추가한다:

```python
@app.command()
def promote_model(
    model_name: str = typer.Option("ctr-model", help="Registry에 등록된 main 모델 이름"),
    champion_alias: str = typer.Option("champion", help="승격 대상 alias"),
    calibration_model_name: str = typer.Option(
        "ctr-calibration-model", help="짝 calibration 모델 이름(downsampling 후보용)"
    ),
) -> None:
    """게이트(지표 비교 + downsampling 페어링) 통과 시 신규 후보를 champion으로 승격."""
    try:
        promoted_version = promote.main(
            model_name=model_name,
            champion_alias=champion_alias,
            calibration_model_name=calibration_model_name,
        )
    except promote.GateRejectedError as exc:
        typer.echo(f"[게이트 미달] {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"[에러] promote-model 실행 중 오류: {exc}", err=True)
        raise typer.Exit(code=1)

    if promoted_version is None:
        typer.echo(f"{model_name}: 평가할 신규 후보 버전 없음 — no-op")
    else:
        typer.echo(f"[OK] {model_name} v{promoted_version} -> @{champion_alias} 승격 완료")
```

- [ ] **Step 4: 테스트 실행 — CLI 테스트 전부 통과 확인**

Run: `uv run python -m pytest tests/test_cli.py -v`
Expected: 전부 PASS(기존 + 새 4개).

- [ ] **Step 5: 커밋**

```bash
git add src/cli.py tests/test_cli.py
git commit -m "feat: promote-model CLI 서브커맨드 배선 (#342)"
```

---

### Task 5: 전체 검증

**Files:** 없음(검증만).

- [ ] **Step 1: 전체 테스트 스위트 실행**

Run: `uv run python -m pytest -v`
Expected: 전부 PASS, 신규 실패 없음.

- [ ] **Step 2: lint 실행**

Run: `uv run --no-sync ruff check autoresearch tests tools`

`.github/workflows/lint.yml`의 `ruff` job과 동일한 명령이다. `src/`는 이
명령의 대상 경로에 포함돼 있지 않으므로(CI도 lint 대상이 아님), `src/cli.py`·
`src/tracking/promote.py`는 이 명령으로 검사되지 않는다 — `tests/test_cli.py`,
`tests/test_tracking_promote.py`는 `tests` 경로에 포함되므로 검사 대상이다.

Expected: 에러 없음.

- [ ] **Step 3: CLI 계약을 수동으로 한 번 확인한다(선택, MLflow 로컬 서버 필요 시)**

로컬 MLflow 서버가 떠 있지 않다면 이 단계는 생략하고 Step 1의 단위 테스트
결과로 계약 충족을 판단한다. 서버가 있다면:

```bash
MLFLOW_TRACKING_URI=http://localhost:5000 uv run python -m src.cli promote-model \
  --model-name ctr-model --champion-alias champion \
  --calibration-model-name ctr-calibration-model
echo "exit code: $?"
```

Expected: 등록된 버전이 없으면 no-op 메시지와 exit code 0.

- [ ] **Step 4: 커밋(필요한 경우에만 — lint 자동수정 등 변경이 생겼을 때)**

```bash
git add -A
git commit -m "chore: promote-model lint 정리 (#342)"
```

변경 사항이 없으면 이 스텝은 건너뛴다.

---

## PR 생성 (Task 5 이후)

- PR 본문에 `Closes #342` 포함.
- 라벨 `feature`, assignee `@me`.
- PR 본문 "리뷰어 참고사항"에 Task 5에서 실행한 검증 명령과 결과를 기록한다.
- 구현 완료 후 이 계획 문서(`docs/plans/2026-07-25-promote-model-champion-gate.md`)는
  `docs/archive/plans/`로 옮긴다(spec은 Airflow가 계속 의존하는 살아있는 CLI
  계약이므로 `docs/specs/`에 유지한다).
