# 학습 데이터셋 스냅샷 GCS 게시·재사용 구현 계획

> **For agentic workers:** REQUIRED SUB-SKILL: `superpowers:subagent-driven-development`(권장) 또는
> `superpowers:executing-plans`로 task 단위 실행. 각 step은 체크박스(`- [ ]`)로 추적한다.

**Goal:** 조립된 학습 데이터셋을 content-addressed 불변 스냅샷으로 GCS에 게시하고,
학습이 `--dataset-uri`로 재조립 없이 그 스냅샷을 소비하게 한다.

**Architecture:** 새 모듈 `training_snapshot_store.py`가 GCS 레이아웃과 write-once
의미론을 독점한다. 조립(`build_training_dataset.main`)이 게시하고, 학습(`train.main`)이
소비한다. manifest 형식은 기존 `training_provenance.py`가 계속 소유한다.

**Tech Stack:** Python 3.11/3.12, pydantic v2, `google-cloud-storage`, typer, pytest

정본 spec: [`docs/specs/2026-08-04-training-dataset-snapshot-store.md`](../specs/2026-08-04-training-dataset-snapshot-store.md) (#530)

## Global Constraints

- 이 저장소는 **한국어 격식체**로 docstring·주석·커밋 메시지를 쓴다.
- 모듈 최상단 docstring에 `[파이프라인]`/`[기능]`/`[비책임]` 3절을 둔다
  (`.claude/docs/agent-python-reference.md` Module Responsibility).
- 모든 함수에 반환 타입을 포함한 타입 힌트를 유지한다.
- 새 pydantic 계약 모델은 `training_provenance._ImmutableModel`을 상속한다
  (`extra="forbid"`, `frozen=True`, `allow_inf_nan=False`).
- **구조 변경과 동작 변경은 별도 커밋으로 분리한다** (CLAUDE.md Core Rules).
- 커밋 메시지: `<type>: <설명>` — type은 `feat`/`fix`/`refactor`/`docs`/`chore`/`test`.
  제목 50자 이내, 현재형 동사로 끝맺는다.
- 시크릿·생성된 데이터 파일·`.env`를 커밋하지 않는다.
- **cross-task 스텁 판별**: 어떤 task 안에 `NotImplementedError`나 미완성으로 보이는
  코드가 있으면, 다음 task가 그것을 채우기로 계획에 적혀 있는지 먼저 확인한다. 그렇다면
  결함이 아니라 정보성 확인 사항으로 분류한다. 이 계획에서 해당하는 것은 Task 3의
  `_update_pointer` 하나뿐이다(Task 4가 채운다).

### 로컬 검증 명령 (이 계획 전체에서 사용)

> **모든 pytest 호출 앞에 `$env:PYTHONUTF8 = "1"`을 붙인다.** PowerShell 셸 상태는
> 호출 간에 유지되지 않으므로 **매 명령마다** 다시 설정해야 한다. 빠뜨리면 이 작업과
> 무관한 cp949 인코딩 실패(#531)가 섞여 들어와 "회귀인가 선재 실패인가" 판단이 흐려진다.

```powershell
$env:PYTHONUTF8 = "1"
uv run --no-sync python -m pytest tests/test_training_snapshot_store.py tests/test_build_training_dataset.py tests/test_build_training_dataset_feast_path.py tests/test_pipeline_train.py tests/test_cli.py tests/test_training_provenance.py tests/test_degradation_eval_hold.py tests/test_degradation_eval_detection.py -q
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
```

**baseline (2026-08-04 측정, 이 브랜치 `0f77c74` 기준): 191 passed, 1 failed.**
유일한 실패는 `tests/test_cli.py::test_promote_model_structured_unexpected_error_emits_safe_stack`으로,
Windows 경로 구분자 문제(`tests/test_cli.py` vs `tests\test_cli.py`)이며 이 작업과 무관하다(#531).
**이 1건 외에 실패가 늘면 그것은 본 작업이 만든 회귀다.**

`PYTHONUTF8=1` 없이 전체를 돌리면 67건이 실패한다 — 전부 Windows 로컬 환경 문제이며 #531이 다룬다.
전체 실행은 9분 걸리므로 반복 검증에는 위 부분집합(3분 45초)을 쓴다.

---

## File Structure

| 파일 | 책임 |
|---|---|
| `src/pipeline/training_snapshot_store.py` (신규) | GCS 레이아웃·write-once 게시·포인터 갱신·다운로드. **이 모듈만 `gs://` 경로 구조를 안다.** |
| `src/pipeline/training_provenance.py` (수정) | `spine_usable_days` 필드, 포인터 pydantic 모델. **형식만 소유하고 I/O는 하지 않는다.** |
| `src/pipeline/build_training_dataset.py` (수정) | `is_experiment_assembly` predicate, `main()`의 게시 호출과 `AssemblyOutcome` 반환 |
| `src/pipeline/train.py` (수정) | `dataset_uri` 다운로드·교차검증·커버리지 게이트 |
| `src/cli.py` (수정) | `--snapshot-root`/`--dataset-uri` 인자, 환경변수 해석, lineage 파라미터 |
| `tests/test_training_snapshot_store.py` (신규) | 스토어 단위 테스트 (fake GCS client) |

---

## Task 1: `is_experiment_assembly` predicate 추출 (구조 변경 전용)

실험 조립 판정이 `require_explicit_experiment_output` 안에 갇혀 있어 게시 게이팅에서
재사용할 수 없다. 조건식이 두 벌 생기지 않도록 먼저 뽑아낸다. **동작 변경 없음.**

**Files:**
- Modify: `src/pipeline/build_training_dataset.py:443-465`
- Test: `tests/test_build_training_dataset.py`

**Interfaces:**
- Produces: `build_training_dataset.is_experiment_assembly(*, feature_service: str | None, extra_features: Sequence[str] | None) -> bool`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_build_training_dataset.py` 끝에 추가:

```python
@pytest.mark.parametrize(
    ("feature_service", "extra_features", "expected"),
    [
        (None, None, False),
        ("ctr_training_v1", None, False),
        ("ctr_training_v1", [], False),
        ("ctr_experiment_v2", None, True),
        (None, ["views_per_day"], True),
        ("ctr_training_v1", ["views_per_day"], True),
    ],
)
def test_is_experiment_assembly_matches_guard_condition(
    feature_service, extra_features, expected
) -> None:
    """predicate가 require_explicit_experiment_output의 판정과 일치해야 한다(#530)."""
    assert (
        build_training_dataset.is_experiment_assembly(
            feature_service=feature_service, extra_features=extra_features
        )
        is expected
    )
```

- [ ] **Step 2: 실패를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_build_training_dataset.py -k is_experiment_assembly -q`
Expected: FAIL — `AttributeError: module 'src.pipeline.build_training_dataset' has no attribute 'is_experiment_assembly'`

- [ ] **Step 3: predicate를 추출하고 가드가 그것을 쓰게 한다**

`src/pipeline/build_training_dataset.py`의 `require_explicit_experiment_output` **바로 앞**에 삽입:

```python
def is_experiment_assembly(
    *,
    feature_service: str | None,
    extra_features: Sequence[str] | None,
) -> bool:
    """이 조립이 prod 기본 조건에서 벗어난 실험 조립인지 판정한다(#530).

    #454가 이 판정을 도입했지만 ``require_explicit_experiment_output`` 안에 갇혀
    있어 다른 곳에서 쓸 수 없었다. 게시 게이팅(#530)이 같은 판정을 필요로 하므로
    predicate로 분리한다 — 조건식이 두 벌이면 ``DEFAULT_SERVICE`` 판별 기준이 바뀔 때
    한쪽만 고치는 실수가 난다.
    """
    from src.features.feast_retrieval import DEFAULT_SERVICE

    experiment_service = (
        feature_service is not None and feature_service != DEFAULT_SERVICE
    )
    return experiment_service or bool(extra_features)
```

같은 파일의 `require_explicit_experiment_output` 본문을 아래로 교체(docstring은 유지):

```python
    if not is_experiment_assembly(
        feature_service=feature_service, extra_features=extra_features
    ):
        return
    raise FeatureContractError(
        "실험 조립(--feature-service 또는 --extra-features)은 출력 경로를 명시해야 "
        "합니다 — 기본 경로는 prod 학습 데이터셋이라 덮어쓰면 이후 prod 학습이 실험 "
        "데이터로 조용히 진행됩니다."
    )
```

기존 함수 안의 `from src.features.feast_retrieval import DEFAULT_SERVICE`와
`experiment_service = ...` 두 줄은 삭제한다(predicate로 옮겨갔다).

- [ ] **Step 4: 통과와 무회귀를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_build_training_dataset.py tests/test_build_training_dataset_feast_path.py -q`
Expected: PASS. 기존 `require_explicit_experiment_output` 테스트가 그대로 통과해야 한다 — 하나라도 깨지면 추출이 동작을 바꾼 것이다.

- [ ] **Step 5: 커밋**

```bash
git add src/pipeline/build_training_dataset.py tests/test_build_training_dataset.py
git commit -m "refactor: 실험 조립 판정을 predicate로 분리한다

게시 게이팅(#530)이 같은 판정을 필요로 하는데 조건식이
require_explicit_experiment_output 안에 갇혀 있었다. 동작은 바뀌지 않는다.

Refs #530"
```

---

## Task 2: manifest 필드와 포인터 모델

**Files:**
- Modify: `src/pipeline/training_provenance.py:96-116` (manifest), `:238-266` (builder)
- Modify: `src/pipeline/build_training_dataset.py:714-721` (builder 호출)
- Test: `tests/test_training_provenance.py`

**Interfaces:**
- Consumes: 없음
- Produces:
  - `TrainingSnapshotManifest.spine_usable_days: NonNegativeInt | None`
  - `build_snapshot_manifest(..., spine_usable_days: int | None = None)`
  - `SnapshotPointerEntry`, `TrainingSnapshotPointer`, `MAX_POINTER_HISTORY: int = 10`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_training_provenance.py`에 추가:

```python
def test_snapshot_manifest_reads_v1_json_without_spine_usable_days() -> None:
    """기존 v1 JSON은 이 필드가 없으므로 None으로 읽어야 한다(#530)."""
    payload = {
        "manifest_version": "training_snapshot_manifest_v1",
        "dataset_sha256": "a" * 64,
        "schema_sha256": "b" * 64,
        "row_count": 10,
        "columns": [{"name": "clicked", "dtype": "int64"}],
        "created_at": "2026-08-04T00:00:00Z",
        "events_start_date": "2026-07-26",
        "events_end_date": "2026-08-01",
        "feature_service": "ctr_training_v1",
        "registry_uri": "gs://bucket/registry.db",
        "registry_generation": "17",
        "registry_sha256": "c" * 64,
    }
    manifest = TrainingSnapshotManifest.model_validate(payload)
    assert manifest.spine_usable_days is None


def test_snapshot_pointer_caps_history_at_ten() -> None:
    """previous는 최근 10개까지만 보존해야 한다(#530)."""
    entries = [
        SnapshotPointerEntry(
            dataset_sha256=f"{index:064d}",
            published_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
        )
        for index in range(12)
    ]
    with pytest.raises(ValidationError):
        TrainingSnapshotPointer(
            dataset_sha256="d" * 64,
            uri="gs://bucket/by-hash/" + "d" * 64 + "/",
            events_start_date=date(2026, 7, 26),
            events_end_date=date(2026, 8, 1),
            feature_service="ctr_training_v1",
            registry_generation="17",
            published_at=datetime(2026, 8, 4, tzinfo=timezone.utc),
            previous=entries,
        )
```

import 절에 `SnapshotPointerEntry`, `TrainingSnapshotPointer`, `MAX_POINTER_HISTORY`를 추가하고,
`datetime`/`timezone`/`date`/`ValidationError`가 이미 import돼 있지 않으면 함께 추가한다.

- [ ] **Step 2: 실패를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_training_provenance.py -k "spine_usable_days or pointer" -q`
Expected: FAIL — `ImportError: cannot import name 'TrainingSnapshotPointer'`

- [ ] **Step 3: 모델을 추가한다**

`src/pipeline/training_provenance.py`의 `TrainingSnapshotManifest` 마지막 필드
(`code_archive_sha: str | None = None`) 뒤에 추가:

```python
    # 기존 v1 snapshot manifest JSON은 이 필드가 없으므로 None으로 읽는다. 재사용
    # 학습(--dataset-uri)은 None이면 커버리지 게이트(#464)를 검증할 수 없어 거부한다.
    # 스키마를 필수로 만들지 않는 이유는 TrainingSplitManifest.experiment_plan_receipt와
    # 같다 — 이 필드와 무관한 기존 소비자가 스키마 변경에 얽히지 않게 한다.
    spine_usable_days: NonNegativeInt | None = None
```

같은 파일 `TrainingSnapshotManifest` 정의 **뒤**에 추가:

```python
MAX_POINTER_HISTORY = 10


class SnapshotPointerEntry(_ImmutableModel):
    """by-date 포인터가 이전에 가리켰던 스냅샷 한 건."""

    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    published_at: datetime


class TrainingSnapshotPointer(_ImmutableModel):
    """(events_end_date, feature_service) 좌표의 최신 스냅샷 포인터.

    by-hash object는 불변이고 재현은 항상 sha 주소로 하므로, 이 포인터가 최신으로
    이동해도 과거 학습은 깨지지 않는다. ``previous``를 캡 없이 두면 매 갱신이 전체를
    읽고 쓰는 비용이 계속 커지므로 최근 ``MAX_POINTER_HISTORY``개만 보존한다.
    """

    pointer_version: Literal["training_snapshot_pointer_v1"] = (
        "training_snapshot_pointer_v1"
    )
    dataset_sha256: str = Field(pattern=SHA256_PATTERN)
    uri: str = Field(min_length=1)
    events_start_date: date
    events_end_date: date
    feature_service: str = Field(min_length=1)
    registry_generation: str = Field(min_length=1)
    published_at: datetime
    previous: list[SnapshotPointerEntry] = Field(
        default_factory=list, max_length=MAX_POINTER_HISTORY
    )
```

`build_snapshot_manifest`에 인자를 더한다 — 시그니처의 `created_at` 앞에:

```python
    spine_usable_days: int | None = None,
```

그리고 `TrainingSnapshotManifest(...)` 생성 인자에 `spine_usable_days=spine_usable_days,`를 추가한다.

- [ ] **Step 4: 조립이 실측 커버리지를 manifest에 싣게 한다**

`src/pipeline/build_training_dataset.py`의 `build_snapshot_manifest(` 호출
(`:714-721`)에 인자를 추가한다:

```python
            manifest = build_snapshot_manifest(
                dataset_path=staged_csv,
                events_start_date=events_start_date,
                events_end_date=events_end_date,
                feature_service=service,
                registry=registry,
                code_archive_sha=os.environ.get("CODE_ARCHIVE_SHA"),
                spine_usable_days=len(coverage.usable_days),
            )
```

`coverage`는 같은 함수 `:622`에서 이미 계산돼 있다. **`_assemble_via_feast`의 시그니처와
반환 타입은 바뀌지 않는다** — 본문에서 인자 하나를 더 넘길 뿐이라 이 함수를 직접 부르는
18개 테스트는 영향받지 않는다.

- [ ] **Step 5: 통과를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_training_provenance.py tests/test_build_training_dataset_feast_path.py -q`
Expected: PASS

- [ ] **Step 6: 커밋**

```bash
git add src/pipeline/training_provenance.py src/pipeline/build_training_dataset.py tests/test_training_provenance.py
git commit -m "feat: snapshot manifest에 커버리지와 포인터 계약 추가

Refs #530"
```

---

## Task 3: 스토어 모듈 — write-once 게시

**Files:**
- Create: `src/pipeline/training_snapshot_store.py`
- Test: `tests/test_training_snapshot_store.py` (신규)

**Interfaces:**
- Consumes: `TrainingSnapshotManifest`, `snapshot_manifest_path`, `sha256_file` (`training_provenance`)
- Produces:
  - `SnapshotStoreError(RuntimeError)`
  - `publish_snapshot(*, dataset_path: Path, snapshot_root: str, record_pointer: bool, client: object | None = None, max_attempts: int = 3) -> str`
    — by-hash prefix URI(`gs://<root>/by-hash/<sha>/`)를 돌려준다

- [ ] **Step 1: 실패하는 테스트와 fake client를 작성한다**

`tests/test_training_snapshot_store.py` 신규:

```python
"""training_snapshot_store의 GCS 레이아웃·write-once 의미론 단위 테스트(#530).

실제 GCS를 부르지 않는다 — _download_pinned_registry와 같은 client 주입 패턴으로
가짜 client를 넘긴다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from src.pipeline import training_snapshot_store as store


class _PreconditionFailed(Exception):
    """google.api_core.exceptions.PreconditionFailed와 같은 code 속성을 갖는다."""

    code = 412


class _FakeBlob:
    def __init__(self, bucket: "_FakeBucket", name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.generation: int | None = None

    def upload_from_filename(self, filename, *, if_generation_match=None, **_):
        self._write(Path(filename).read_bytes(), if_generation_match)

    def upload_from_string(self, data, *, if_generation_match=None, **_):
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._write(payload, if_generation_match)

    def _write(self, payload: bytes, if_generation_match) -> None:
        existing = self._bucket.objects.get(self.name)
        if if_generation_match == 0 and existing is not None:
            raise _PreconditionFailed("object already exists")
        if if_generation_match not in (None, 0):
            if existing is None or existing[1] != if_generation_match:
                raise _PreconditionFailed("generation mismatch")
        self._bucket.generation += 1
        self._bucket.objects[self.name] = (payload, self._bucket.generation)
        self.generation = self._bucket.generation

    def download_as_bytes(self) -> bytes:
        return self._bucket.objects[self.name][0]

    def download_to_filename(self, filename) -> None:
        Path(filename).write_bytes(self.download_as_bytes())

    def reload(self) -> None:
        entry = self._bucket.objects.get(self.name)
        if entry is None:
            raise FileNotFoundError(self.name)
        self.generation = entry[1]

    def exists(self) -> bool:
        return self.name in self._bucket.objects


class _FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.generation = 0

    def blob(self, name: str, **_) -> _FakeBlob:
        return _FakeBlob(self, name)


class _FakeClient:
    def __init__(self) -> None:
        self.buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return self.buckets.setdefault(name, _FakeBucket())


def _write_dataset(tmp_path: Path) -> Path:
    """CSV와 대응 sidecar를 만든 뒤 CSV 경로를 돌려준다."""
    from src.pipeline.training_provenance import (
        build_snapshot_manifest,
        RegistryProvenance,
        snapshot_manifest_path,
        write_manifest_atomic,
    )

    csv_path = tmp_path / "training_dataset.csv"
    csv_path.write_text("clicked\n1\n0\n", encoding="utf-8")
    manifest = build_snapshot_manifest(
        dataset_path=csv_path,
        events_start_date="2026-07-26",
        events_end_date="2026-08-01",
        feature_service="ctr_training_v1",
        registry=RegistryProvenance(
            uri="gs://bucket/registry.db", generation="17", sha256="c" * 64
        ),
        code_archive_sha=None,
        spine_usable_days=7,
    )
    write_manifest_atomic(manifest, snapshot_manifest_path(csv_path))
    return csv_path


def test_publish_writes_csv_and_manifest_under_content_address(tmp_path) -> None:
    """by-hash/<sha>/ 밑에 CSV와 manifest가 올라가야 한다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()

    uri = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )

    from src.pipeline.training_provenance import sha256_file

    sha = sha256_file(csv_path)
    assert uri == f"gs://snapshots/training/by-hash/{sha}/"
    bucket = client.buckets["snapshots"]
    assert f"training/by-hash/{sha}/training_dataset.csv" in bucket.objects
    assert f"training/by-hash/{sha}/snapshot_manifest.json" in bucket.objects


def test_publish_is_idempotent_for_identical_input(tmp_path) -> None:
    """같은 내용을 다시 게시하면 412를 no-op으로 흡수해야 한다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()

    first = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )
    second = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )

    assert first == second
    assert client.buckets["snapshots"].generation == 2  # 두 번째는 아무것도 안 씀


def test_publish_raises_after_exhausting_retries(tmp_path, monkeypatch) -> None:
    """일시 장애가 계속되면 재시도를 소진하고 실패해야 한다."""
    csv_path = _write_dataset(tmp_path)

    class _AlwaysFailing(_FakeClient):
        def bucket(self, name: str):
            raise RuntimeError("transient GCS failure")

    monkeypatch.setattr(store.time, "sleep", lambda _seconds: None)
    with pytest.raises(store.SnapshotStoreError) as error:
        store.publish_snapshot(
            dataset_path=csv_path,
            snapshot_root="gs://snapshots/training",
            record_pointer=False,
            client=_AlwaysFailing(),
            max_attempts=3,
        )
    assert str(csv_path) in str(error.value)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_training_snapshot_store.py -q`
Expected: FAIL — `ModuleNotFoundError: No module named 'src.pipeline.training_snapshot_store'`

- [ ] **Step 3: 스토어 모듈을 만든다**

`src/pipeline/training_snapshot_store.py` 신규:

```python
"""학습 데이터셋 스냅샷의 GCS 게시·조회 스토어(#530).

[파이프라인] 조립이 만든 CSV·sidecar를 content-addressed 불변 객체로 GCS에 올리고,
재사용 학습이 그것을 다시 내려받는 구간을 담당한다.

[기능] ``gs://<root>/by-hash/<dataset_sha256>/`` 레이아웃, ``if_generation_match=0``
기반 write-once 게시(이미 있으면 no-op), ``by-date/dt=<날짜>/<service>.json`` 포인터의
read-modify-write 갱신, 스냅샷 다운로드를 제공한다. 재시도 단위는 **게시 호출 전체**다 —
write-once가 412를 no-op으로 흡수하므로 CSV만 올라간 상태에서 재시도하면 manifest부터
자연스럽게 이어진다.

[비책임] 데이터셋 조립(build_training_dataset), 모델 학습(train), manifest 형식
정의(training_provenance)는 다루지 않는다. 게시 여부 판단(실험 조립인지, 루트가
지정됐는지)도 호출부의 몫이다.
"""

from __future__ import annotations

import time
from pathlib import Path
from urllib.parse import urlparse

from src.pipeline.training_provenance import (
    TrainingSnapshotManifest,
    load_training_snapshot_manifest,
    sha256_file,
    snapshot_manifest_path,
)

CSV_OBJECT_NAME = "training_dataset.csv"
MANIFEST_OBJECT_NAME = "snapshot_manifest.json"
_PRECONDITION_FAILED = 412
_RETRY_BASE_SECONDS = 1.0


class SnapshotStoreError(RuntimeError):
    """스냅샷 게시 또는 조회가 확정적으로 실패했음을 알리는 오류."""


def _parse_root(root: str) -> tuple[str, str]:
    """gs://bucket/prefix를 (bucket, prefix)로 나눈다."""
    parsed = urlparse(root)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise SnapshotStoreError(
            f"snapshot root는 gs://bucket[/prefix] 형식이어야 합니다: {root}"
        )
    return parsed.netloc, parsed.path.strip("/")


def _join(prefix: str, *parts: str) -> str:
    return "/".join([segment for segment in (prefix, *parts) if segment])


def _is_precondition_failure(error: BaseException) -> bool:
    """write-once 전제조건 위반(이미 존재)인지 판정한다."""
    return getattr(error, "code", None) == _PRECONDITION_FAILED


def _resolve_client(client: object | None) -> object:
    if client is not None:
        return client
    from google.cloud import storage

    return storage.Client()


def _upload_once(bucket: object, name: str, *, path: Path | None, text: str | None) -> None:
    """write-once로 올리되 이미 있으면 no-op으로 흡수한다."""
    blob = bucket.blob(name)
    try:
        if path is not None:
            blob.upload_from_filename(str(path), if_generation_match=0)
        else:
            blob.upload_from_string(
                text, content_type="application/json", if_generation_match=0
            )
    except Exception as error:
        if _is_precondition_failure(error):
            return
        raise


def publish_snapshot(
    *,
    dataset_path: Path,
    snapshot_root: str,
    record_pointer: bool,
    client: object | None = None,
    max_attempts: int = 3,
) -> str:
    """CSV와 sidecar를 content-addressed 주소에 게시하고 by-hash prefix URI를 돌려준다.

    Args:
        record_pointer: by-date 포인터도 갱신할지. 실험 조립은 False로 넘겨야 한다 —
            prod 포인터를 오염시키지 않기 위해서다(#530 §6.3).
        max_attempts: 게시 호출 전체의 재시도 횟수.

    Raises:
        SnapshotStoreError: 재시도를 소진하고도 실패하면. 로컬 CSV는 유효하게 남아
            있으므로 메시지에 그 경로를 싣는다.
    """
    manifest = load_training_snapshot_manifest(dataset_path)
    dataset_sha256 = sha256_file(dataset_path)
    bucket_name, prefix = _parse_root(snapshot_root)
    object_prefix = _join(prefix, "by-hash", dataset_sha256)
    uri = f"gs://{bucket_name}/{object_prefix}/"

    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resolved = _resolve_client(client)
            bucket = resolved.bucket(bucket_name)
            _upload_once(
                bucket, f"{object_prefix}/{CSV_OBJECT_NAME}", path=dataset_path, text=None
            )
            _upload_once(
                bucket,
                f"{object_prefix}/{MANIFEST_OBJECT_NAME}",
                path=snapshot_manifest_path(dataset_path),
                text=None,
            )
            if record_pointer:
                _update_pointer(
                    bucket,
                    prefix=prefix,
                    manifest=manifest,
                    dataset_sha256=dataset_sha256,
                    uri=uri,
                )
            return uri
        except Exception as error:  # noqa: BLE001 - 재시도 후 확정 실패로 감싼다
            last_error = error
            if attempt < max_attempts:
                time.sleep(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

    raise SnapshotStoreError(
        f"스냅샷 게시가 {max_attempts}회 시도 후 실패했습니다: {uri}. "
        f"로컬 CSV는 유효하게 저장돼 있습니다: {dataset_path} "
        "(파드가 살아있는 동안 이 파일을 직접 회수할 수 있습니다)."
    ) from last_error
```

`_update_pointer`는 Task 4에서 구현한다. 지금은 파일 하단에 아래 스텁을 둔다.

> **이 스텁은 의도된 것이며 결함이 아니다.** Task 3의 테스트는 전부
> `record_pointer=False`로만 호출하므로 이 스텁은 Task 3 안에서 실행 경로를 한 번도
> 타지 않는다. 그리고 Task 4 Step 2가 기대하는 실패가 정확히
> `NotImplementedError: Task 4에서 구현한다`이다 — 이 스텁이 Task 4의 TDD RED 지점
> 자체다. 없애면 Task 4의 RED 단계가 성립하지 않는다. 리뷰는 이 항목을
> Important/Minor가 아니라 정보성 확인 사항으로 분류한다.

```python
def _update_pointer(bucket, *, prefix, manifest, dataset_sha256, uri) -> None:
    raise NotImplementedError("Task 4에서 구현한다")
```

- [ ] **Step 4: 통과를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_training_snapshot_store.py -q`
Expected: PASS (3 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/pipeline/training_snapshot_store.py tests/test_training_snapshot_store.py
git commit -m "feat: content-addressed 스냅샷 게시 스토어를 추가한다

Refs #530"
```

---

## Task 4: by-date 포인터 read-modify-write

**Files:**
- Modify: `src/pipeline/training_snapshot_store.py`
- Test: `tests/test_training_snapshot_store.py`

**Interfaces:**
- Consumes: `TrainingSnapshotPointer`, `SnapshotPointerEntry`, `MAX_POINTER_HISTORY` (Task 2)
- Produces: `_update_pointer(bucket, *, prefix, manifest, dataset_sha256, uri) -> None` (모듈 내부)

- [ ] **Step 1: 실패하는 테스트를 작성한다**

```python
def test_pointer_records_latest_and_keeps_previous(tmp_path) -> None:
    """재조립으로 sha가 바뀌면 포인터가 최신을 가리키고 이전 sha를 previous에 남긴다."""
    client = _FakeClient()
    first = _write_dataset(tmp_path)
    store.publish_snapshot(
        dataset_path=first,
        snapshot_root="gs://snapshots/training",
        record_pointer=True,
        client=client,
    )

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _write_dataset(second_dir)
    second.write_text("clicked\n1\n1\n1\n", encoding="utf-8")
    _republish_sidecar(second)
    store.publish_snapshot(
        dataset_path=second,
        snapshot_root="gs://snapshots/training",
        record_pointer=True,
        client=client,
    )

    from src.pipeline.training_provenance import sha256_file

    payload = json.loads(
        client.buckets["snapshots"]
        .objects["training/by-date/dt=2026-08-01/ctr_training_v1.json"][0]
        .decode("utf-8")
    )
    assert payload["dataset_sha256"] == sha256_file(second)
    assert [entry["dataset_sha256"] for entry in payload["previous"]] == [
        sha256_file(first)
    ]


def test_pointer_history_is_capped(tmp_path) -> None:
    """previous는 MAX_POINTER_HISTORY개를 넘지 않는다."""
    from src.pipeline.training_provenance import MAX_POINTER_HISTORY

    client = _FakeClient()
    for index in range(MAX_POINTER_HISTORY + 3):
        run_dir = tmp_path / f"run{index}"
        run_dir.mkdir()
        csv_path = _write_dataset(run_dir)
        csv_path.write_text("clicked\n" + "1\n" * (index + 1), encoding="utf-8")
        _republish_sidecar(csv_path)
        store.publish_snapshot(
            dataset_path=csv_path,
            snapshot_root="gs://snapshots/training",
            record_pointer=True,
            client=client,
        )

    payload = json.loads(
        client.buckets["snapshots"]
        .objects["training/by-date/dt=2026-08-01/ctr_training_v1.json"][0]
        .decode("utf-8")
    )
    assert len(payload["previous"]) == MAX_POINTER_HISTORY


def test_experiment_assembly_does_not_touch_pointer(tmp_path) -> None:
    """record_pointer=False면 by-date 객체가 아예 생기지 않는다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )
    assert not any(
        name.startswith("training/by-date/")
        for name in client.buckets["snapshots"].objects
    )
```

파일 상단 헬퍼에 추가(CSV를 고친 뒤 sidecar를 다시 만든다):

```python
def _republish_sidecar(csv_path: Path) -> None:
    """CSV를 수정한 뒤 sha가 맞는 sidecar를 다시 쓴다."""
    from src.pipeline.training_provenance import (
        build_snapshot_manifest,
        RegistryProvenance,
        snapshot_manifest_path,
        write_manifest_atomic,
    )

    manifest = build_snapshot_manifest(
        dataset_path=csv_path,
        events_start_date="2026-07-26",
        events_end_date="2026-08-01",
        feature_service="ctr_training_v1",
        registry=RegistryProvenance(
            uri="gs://bucket/registry.db", generation="17", sha256="c" * 64
        ),
        code_archive_sha=None,
        spine_usable_days=7,
    )
    write_manifest_atomic(manifest, snapshot_manifest_path(csv_path))
```

- [ ] **Step 2: 실패를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_training_snapshot_store.py -k pointer -q`
Expected: FAIL — `NotImplementedError: Task 4에서 구현한다`

- [ ] **Step 3: 포인터 갱신을 구현한다**

`training_snapshot_store.py`의 스텁을 아래로 교체하고, import에
`SnapshotPointerEntry`, `TrainingSnapshotPointer`, `MAX_POINTER_HISTORY`를 추가한다:

```python
def _pointer_object_name(prefix: str, manifest: TrainingSnapshotManifest) -> str:
    return _join(
        prefix,
        "by-date",
        f"dt={manifest.events_end_date.isoformat()}",
        f"{manifest.feature_service}.json",
    )


def _update_pointer(
    bucket: object,
    *,
    prefix: str,
    manifest: TrainingSnapshotManifest,
    dataset_sha256: str,
    uri: str,
) -> None:
    """by-date 포인터를 최신 스냅샷으로 갱신한다.

    read-modify-write에 generation 전제조건을 걸어, 같은 좌표에 동시에 쓰는 실행이
    서로의 갱신을 덮어쓰지 않게 한다. 경합하면 호출부의 재시도가 다시 읽고 시도한다.
    """
    name = _pointer_object_name(prefix, manifest)
    blob = bucket.blob(name)

    current: TrainingSnapshotPointer | None = None
    generation: int | None = None
    try:
        blob.reload()
        generation = blob.generation
        current = TrainingSnapshotPointer.model_validate_json(
            blob.download_as_bytes().decode("utf-8")
        )
    except Exception as error:  # noqa: BLE001 - 최초 게시는 객체가 없는 게 정상이다
        if _is_precondition_failure(error):
            raise
        current = None
        generation = None

    if current is not None and current.dataset_sha256 == dataset_sha256:
        return

    history: list[SnapshotPointerEntry] = []
    if current is not None:
        history = [
            SnapshotPointerEntry(
                dataset_sha256=current.dataset_sha256,
                published_at=current.published_at,
            ),
            *current.previous,
        ][:MAX_POINTER_HISTORY]

    pointer = TrainingSnapshotPointer(
        dataset_sha256=dataset_sha256,
        uri=uri,
        events_start_date=manifest.events_start_date,
        events_end_date=manifest.events_end_date,
        feature_service=manifest.feature_service,
        registry_generation=manifest.registry_generation,
        published_at=manifest.created_at,
        previous=history,
    )
    bucket.blob(name).upload_from_string(
        pointer.model_dump_json(indent=2),
        content_type="application/json",
        if_generation_match=0 if generation is None else generation,
    )
```

- [ ] **Step 4: 통과를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_training_snapshot_store.py -q`
Expected: PASS (6 tests)

- [ ] **Step 5: 커밋**

```bash
git add src/pipeline/training_snapshot_store.py tests/test_training_snapshot_store.py
git commit -m "feat: by-date 스냅샷 포인터를 generation 전제조건으로 갱신

Refs #530"
```

---

## Task 5: 조립이 게시하고 `AssemblyOutcome`을 반환

**Files:**
- Modify: `src/pipeline/build_training_dataset.py:861-911` (`main`)
- Test: `tests/test_build_training_dataset.py`

**Interfaces:**
- Consumes: `publish_snapshot` (Task 3), `is_experiment_assembly` (Task 1)
- Produces: `AssemblyOutcome(coverage: SpineCoverage, snapshot_uri: str | None)`,
  `main(..., snapshot_root: str | None = None) -> AssemblyOutcome`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

```python
def test_main_publishes_only_when_snapshot_root_given(tmp_path, monkeypatch) -> None:
    """루트를 명시했을 때만 게시한다 — degradation_eval처럼 안 넘기는 호출은 게시 경로에 못 든다."""
    calls: list[dict] = []
    monkeypatch.setattr(
        build_training_dataset,
        "_assemble_via_feast",
        lambda *args, **kwargs: _COVERAGE_STUB,
    )
    monkeypatch.setattr(
        build_training_dataset.training_snapshot_store,
        "publish_snapshot",
        lambda **kwargs: calls.append(kwargs) or "gs://snapshots/by-hash/abc/",
    )

    without = build_training_dataset.main(
        output_path=str(tmp_path / "a.csv"),
        events_start_date="2026-07-26",
        events_end_date="2026-08-01",
    )
    assert without.snapshot_uri is None
    assert calls == []

    with_root = build_training_dataset.main(
        output_path=str(tmp_path / "b.csv"),
        events_start_date="2026-07-26",
        events_end_date="2026-08-01",
        snapshot_root="gs://snapshots/training",
    )
    assert with_root.snapshot_uri == "gs://snapshots/by-hash/abc/"
    assert calls[0]["record_pointer"] is True


def test_main_skips_pointer_for_experiment_assembly(tmp_path, monkeypatch) -> None:
    """실험 조립은 루트가 켜져 있어도 prod 포인터를 건드리지 않는다."""
    calls: list[dict] = []
    monkeypatch.setattr(
        build_training_dataset,
        "_assemble_via_feast",
        lambda *args, **kwargs: _COVERAGE_STUB,
    )
    monkeypatch.setattr(
        build_training_dataset.training_snapshot_store,
        "publish_snapshot",
        lambda **kwargs: calls.append(kwargs) or "gs://snapshots/by-hash/abc/",
    )

    build_training_dataset.main(
        output_path=str(tmp_path / "exp.csv"),
        events_start_date="2026-07-26",
        events_end_date="2026-08-01",
        snapshot_root="gs://snapshots/training",
        extra_features=["views_per_day"],
    )
    assert calls[0]["record_pointer"] is False
```

`_COVERAGE_STUB`은 파일 상단에 정의한다:

```python
_COVERAGE_STUB = build_training_dataset.SpineCoverage(
    requested_days=("2026-08-01",),
    usable_days=("2026-08-01",),
    sparse_days=(),
    missing_days=(),
    zero_click_days=(),
    total_rows=2,
    total_clicks=1,
)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_build_training_dataset.py -k "publishes_only or skips_pointer" -q`
Expected: FAIL — `TypeError: main() got an unexpected keyword argument 'snapshot_root'`

- [ ] **Step 3: `main()`을 고친다**

`build_training_dataset.py` import 절에 추가:

```python
from src.pipeline import training_snapshot_store
```

`SpineCoverage` 정의 뒤에 추가:

```python
@dataclass(frozen=True)
class AssemblyOutcome:
    """조립 결과 — 실측 커버리지와 게시된 스냅샷 주소(#530).

    ``train.py``의 ``TrainingOutcome``과 같은 패턴이다. ``snapshot_uri``는 게시하지
    않은 실행에서 ``None``이며, 호출부는 이 값을 MLflow lineage에 남긴다.
    """

    coverage: SpineCoverage
    snapshot_uri: str | None = None
```

`main()`의 시그니처에 인자를 더한다(`extra_features` 뒤):

```python
    snapshot_root: str | None = None,
```

`main()`의 마지막 `return _assemble_via_feast(...)`를 아래로 교체:

```python
    coverage = _assemble_via_feast(
        output_path,
        events_start_date,
        events_end_date,
        min_coverage_days=min_coverage_days,
        feature_service=feature_service,
        extra_features=extra_features,
    )
    if snapshot_root is None:
        print("[게시 없음] snapshot root 미지정 — 로컬에만 저장")
        return AssemblyOutcome(coverage=coverage, snapshot_uri=None)

    # 실험 조립은 by-hash에만 올리고 by-date 포인터는 건드리지 않는다(#530 §6.3) —
    # 누가 실험 스크립트에 루트를 켜도 prod 포인터가 오염되지 않게 한다.
    snapshot_uri = training_snapshot_store.publish_snapshot(
        dataset_path=Path(output_path),
        snapshot_root=snapshot_root,
        record_pointer=not is_experiment_assembly(
            feature_service=feature_service, extra_features=extra_features
        ),
    )
    print(f"[게시] {snapshot_uri}")
    return AssemblyOutcome(coverage=coverage, snapshot_uri=snapshot_uri)
```

`main()`의 docstring `Returns:` 절을 `AssemblyOutcome`으로 갱신하고, `snapshot_root`
인자 설명을 `Args:`에 추가한다. 모듈 최상단 docstring의 `[기능]` 절에도 게시 책임을 한 줄 더한다.

- [ ] **Step 4: `run-pipeline` 호출부가 깨지지 않게 맞춘다**

`src/cli.py:445`의 `coverage = build_training_dataset.main(...)`를
`assembly = build_training_dataset.main(...)`로 바꾸고, `:478`의
`coverage.as_lineage_params(...)`를 `assembly.coverage.as_lineage_params(...)`로 바꾼다.
`degradation_eval.py:759`/`:806`과 `cli.py:115`는 반환값을 버리므로 무변경이다.

- [ ] **Step 5: 통과를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_build_training_dataset.py tests/test_build_training_dataset_feast_path.py tests/test_cli.py tests/test_degradation_eval_hold.py tests/test_degradation_eval_detection.py -q`
Expected: baseline과 같은 1 failed(경로 구분자)만 남는다.

- [ ] **Step 6: 커밋**

```bash
git add src/pipeline/build_training_dataset.py src/cli.py tests/test_build_training_dataset.py
git commit -m "feat: 조립이 스냅샷을 게시하고 AssemblyOutcome을 반환한다

Refs #530"
```

---

## Task 6: CLI가 루트를 해석한다

환경변수는 **`cli.py`에서만** 읽는다. `main()`이 직접 읽으면 `degradation_eval`의
horizon 루프(`:806`)가 평가일 수만큼 포인터를 갱신해 prod 포인터를 덮어쓴다.

**Files:**
- Modify: `src/cli.py:74-124` (`build_features`), `:348-422` (`run_pipeline`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Produces: `_snapshot_root_kwargs(snapshot_root: object) -> dict`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

```python
def test_snapshot_root_falls_back_to_environment(monkeypatch) -> None:
    """--snapshot-root 미지정 시 TRAINING_SNAPSHOT_ROOT를 쓴다(#530)."""
    monkeypatch.setenv("TRAINING_SNAPSHOT_ROOT", "gs://snapshots/training")
    assert cli._snapshot_root_kwargs(None) == {
        "snapshot_root": "gs://snapshots/training"
    }


def test_snapshot_root_option_wins_over_environment(monkeypatch) -> None:
    monkeypatch.setenv("TRAINING_SNAPSHOT_ROOT", "gs://from-env/training")
    assert cli._snapshot_root_kwargs("gs://explicit/training") == {
        "snapshot_root": "gs://explicit/training"
    }


def test_snapshot_root_absent_yields_no_kwarg(monkeypatch) -> None:
    """미설정이면 키 자체를 만들지 않아 main()의 기본값을 덮지 않는다."""
    monkeypatch.delenv("TRAINING_SNAPSHOT_ROOT", raising=False)
    assert cli._snapshot_root_kwargs(None) == {}
```

- [ ] **Step 2: 실패를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_cli.py -k snapshot_root -q`
Expected: FAIL — `AttributeError: module 'src.cli' has no attribute '_snapshot_root_kwargs'`

- [ ] **Step 3: 구현한다**

`src/cli.py`의 `_assembly_feature_kwargs` 뒤에 추가:

```python
def _snapshot_root_kwargs(snapshot_root: object) -> dict:
    """스냅샷 게시 루트를 옵션 → 환경변수 순으로 해석한다(#530).

    환경변수를 `build_training_dataset.main()`이 아니라 여기서만 읽는 것이 계약이다 —
    `degradation_eval`의 horizon 평가 루프는 `main()`을 평가일 수만큼 부르므로,
    `main()`이 환경변수를 직접 읽으면 그 루프가 by-date 포인터를 평가일마다 덮어쓴다.
    루트를 명시적으로 넘기지 않는 호출은 게시 경로에 들어갈 방법이 없어야 한다.
    """
    resolved = _optional_cli_string(snapshot_root) or os.environ.get(
        "TRAINING_SNAPSHOT_ROOT"
    )
    return {} if not resolved else {"snapshot_root": resolved}
```

`build_features`와 `run_pipeline` 양쪽에 옵션을 추가한다:

```python
    snapshot_root: Optional[str] = typer.Option(
        None,
        "--snapshot-root",
        help=(
            "조립한 데이터셋을 게시할 gs://bucket/prefix (미지정 시 "
            "TRAINING_SNAPSHOT_ROOT, 둘 다 없으면 게시하지 않습니다). "
            "prod 재학습 경로에만 지정하십시오 — 실험·dev 파이프라인이 켜면 "
            "by-date 포인터가 경합합니다(#530)."
        ),
    ),
```

두 명령의 `build_training_dataset.main(...)` 호출에 `**_snapshot_root_kwargs(snapshot_root)`를 더한다.

- [ ] **Step 4: 통과를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_cli.py -q`
Expected: baseline과 같은 1 failed만 남는다.

- [ ] **Step 5: 커밋**

```bash
git add src/cli.py tests/test_cli.py
git commit -m "feat: 조립 CLI에 --snapshot-root 옵션을 추가한다

Refs #530"
```

---

## Task 7: 스냅샷 다운로드와 재검증

**Files:**
- Modify: `src/pipeline/training_snapshot_store.py`, `src/pipeline/train.py:403-478`
- Test: `tests/test_training_snapshot_store.py`, `tests/test_pipeline_train.py`

**Interfaces:**
- Produces:
  - `download_snapshot(*, dataset_uri: str, destination_dir: Path, client: object | None = None) -> Path`
  - `train.main(..., dataset_uri: str | None = None)`

- [ ] **Step 1: 실패하는 테스트를 작성한다**

`tests/test_training_snapshot_store.py`에 추가:

```python
def test_download_restores_sidecar_naming(tmp_path) -> None:
    """sidecar를 load_training_snapshot_manifest가 기대하는 이름으로 내려받아야 한다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    uri = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )

    destination = tmp_path / "download"
    destination.mkdir()
    local = store.download_snapshot(
        dataset_uri=uri, destination_dir=destination, client=client
    )

    from src.pipeline.training_provenance import load_training_snapshot_manifest

    assert local.name == "training_dataset.csv"
    assert (destination / "training_dataset.csv.snapshot.json").is_file()
    assert load_training_snapshot_manifest(local).spine_usable_days == 7


def test_download_rejects_sha_mismatch_between_uri_and_manifest(tmp_path) -> None:
    """URI의 sha와 manifest.dataset_sha256이 다르면 거부한다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )
    destination = tmp_path / "download"
    destination.mkdir()

    with pytest.raises(store.SnapshotStoreError):
        store.download_snapshot(
            dataset_uri="gs://snapshots/training/by-hash/" + "f" * 64 + "/",
            destination_dir=destination,
            client=client,
        )
```

`tests/test_pipeline_train.py`에 추가:

```python
def test_train_rejects_snapshot_without_coverage_when_gate_on(tmp_path, monkeypatch) -> None:
    """spine_usable_days가 없는 스냅샷은 커버리지 게이트를 검증할 수 없어 거부한다(#530)."""
    from src.pipeline.training_provenance import ProvenanceValidationError

    with pytest.raises(ProvenanceValidationError, match="커버리지"):
        train.require_snapshot_coverage(spine_usable_days=None, min_days=3)


def test_train_rejects_snapshot_below_coverage_floor() -> None:
    from src.pipeline.training_provenance import ProvenanceValidationError

    with pytest.raises(ProvenanceValidationError, match="2일"):
        train.require_snapshot_coverage(spine_usable_days=2, min_days=3)


def test_train_accepts_snapshot_when_gate_off() -> None:
    """min_days<=0은 명시적 우회구다 — None이어도 통과시킨다."""
    train.require_snapshot_coverage(spine_usable_days=None, min_days=0)
```

- [ ] **Step 2: 실패를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_training_snapshot_store.py tests/test_pipeline_train.py -k "download or coverage" -q`
Expected: FAIL — `AttributeError: ... has no attribute 'download_snapshot'` / `'require_snapshot_coverage'`

- [ ] **Step 3: 다운로드를 구현한다**

`training_snapshot_store.py`에 추가:

```python
def download_snapshot(
    *,
    dataset_uri: str,
    destination_dir: Path,
    client: object | None = None,
) -> Path:
    """by-hash 스냅샷을 내려받아 로컬 CSV 경로를 돌려준다.

    sidecar는 ``snapshot_manifest_path()``가 기대하는 ``<csv>.snapshot.json`` 이름으로
    복원한다 — 그래야 기존 ``load_training_snapshot_manifest()``가 그대로 재사용되고
    byte/schema/row_count 재검증이 따라온다.

    Raises:
        SnapshotStoreError: URI 형식이 틀렸거나, URI의 sha와 manifest의
            ``dataset_sha256``이 다르면. content-addressing을 신뢰할 근거가 여기서 생긴다.
    """
    parsed = urlparse(dataset_uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise SnapshotStoreError(
            f"dataset URI는 gs://bucket/... 형식이어야 합니다: {dataset_uri}"
        )
    object_prefix = parsed.path.strip("/")
    segments = object_prefix.split("/")
    if len(segments) < 2 or segments[-2] != "by-hash":
        raise SnapshotStoreError(
            f"dataset URI는 by-hash/<sha>/ 로 끝나야 합니다: {dataset_uri}"
        )
    expected_sha = segments[-1]

    resolved = _resolve_client(client)
    bucket = resolved.bucket(parsed.netloc)
    csv_path = destination_dir / CSV_OBJECT_NAME
    bucket.blob(f"{object_prefix}/{CSV_OBJECT_NAME}").download_to_filename(str(csv_path))
    sidecar = snapshot_manifest_path(csv_path)
    bucket.blob(f"{object_prefix}/{MANIFEST_OBJECT_NAME}").download_to_filename(
        str(sidecar)
    )

    manifest = load_training_snapshot_manifest(csv_path)
    if manifest.dataset_sha256 != expected_sha:
        raise SnapshotStoreError(
            "스냅샷 주소와 manifest의 dataset_sha256이 다릅니다 — "
            f"주소={expected_sha}, manifest={manifest.dataset_sha256}"
        )
    return csv_path
```

`train.py`에 커버리지 게이트를 추가한다(`_log_reproducibility_artifacts` 앞):

```python
def require_snapshot_coverage(*, spine_usable_days: int | None, min_days: int) -> None:
    """재사용 스냅샷이 커버리지 하한을 만족하는지 검증한다(#530).

    ``require_spine_coverage``(#464)와 같은 술어를 manifest 값으로 재현한다. 재조립을
    하지 않는 경로라 실측 coverage 객체가 없으므로 sidecar가 실은 값을 쓴다.
    ``None``은 이 필드가 없던 시절 manifest라는 뜻이며, 게이트가 켜져 있으면 검증할
    근거가 없으므로 조용히 통과시키지 않는다.
    """
    if min_days <= 0:
        return
    if spine_usable_days is None:
        raise ProvenanceValidationError(
            "스냅샷에 spine 커버리지 기록이 없어 커버리지 게이트를 검증할 수 없습니다 "
            f"(최소 {min_days}일 필요). 데이터셋을 다시 조립하거나 "
            "min_coverage_days=0으로 명시적으로 우회하십시오."
        )
    if spine_usable_days < min_days:
        raise ProvenanceValidationError(
            f"스냅샷의 사용 가능한 날이 {spine_usable_days}일로 최소 {min_days}일에 "
            "미달합니다. 기간을 넓혀 재조립하거나 min_coverage_days=0으로 우회하십시오."
        )
```

`train.py` import 절에 `from src.pipeline import training_snapshot_store`를 추가한다
(`TemporaryDirectory`는 `:45`에 이미 import돼 있다).

`train.main()`에 `dataset_uri: str | None = None`, `min_coverage_days: int = 0` 인자를 더하고,
`data_path` 해석(`:466-469`) **앞**에 삽입:

```python
    snapshot_download_dir: TemporaryDirectory | None = None
    if dataset_uri is not None:
        if data_path is not None:
            raise ValueError(
                "dataset_uri와 data_path는 함께 지정할 수 없습니다 — "
                "어느 쪽이 학습 입력인지 결정할 수 없습니다"
            )
        snapshot_download_dir = TemporaryDirectory(prefix="training_snapshot_")
        data_path = str(
            training_snapshot_store.download_snapshot(
                dataset_uri=dataset_uri,
                destination_dir=Path(snapshot_download_dir.name),
            )
        )
```

`snapshot_manifest`를 읽은 직후(`:478` 뒤)에 게이트를 걸고 lineage를 채운다:

```python
    if dataset_uri is not None and snapshot_manifest is not None:
        require_snapshot_coverage(
            spine_usable_days=snapshot_manifest.spine_usable_days,
            min_days=min_coverage_days,
        )
        # 재조립을 하지 않은 실행도 "어떤 조건으로 만든 데이터인가"를 run 파라미터만
        # 보고 판별할 수 있어야 한다(#530 §7.2). 조립 반환값이 없는 대신 sidecar가
        # 같은 값을 전부 싣고 있으므로 거기서 채운다. 호출부(cli)가 아니라 여기서
        # 채우는 이유는, manifest를 실제로 읽는 주체가 이 함수뿐이기 때문이다.
        reuse_params = {
            "events_start_date": snapshot_manifest.events_start_date.isoformat(),
            "events_end_date": snapshot_manifest.events_end_date.isoformat(),
            "feature_service": snapshot_manifest.feature_service,
            "feast_registry_path": snapshot_manifest.registry_uri,
            "feast_registry_generation": snapshot_manifest.registry_generation,
        }
        if snapshot_manifest.spine_usable_days is not None:
            reuse_params["spine_usable_days"] = str(snapshot_manifest.spine_usable_days)
        extra_params = {**(extra_params or {}), **reuse_params}
```

`main()` 끝의 정리에서 `snapshot_download_dir`가 있으면 `cleanup()`을 부른다
(`try/finally`로 감싸 학습이 실패해도 임시 디렉터리가 남지 않게 한다).

- [ ] **Step 4: 통과를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_training_snapshot_store.py tests/test_pipeline_train.py -q`
Expected: PASS

- [ ] **Step 5: 커밋**

```bash
git add src/pipeline/training_snapshot_store.py src/pipeline/train.py tests/test_training_snapshot_store.py tests/test_pipeline_train.py
git commit -m "feat: 스냅샷 다운로드와 재사용 학습 커버리지 게이트를 추가한다

Refs #530"
```

---

## Task 8: CLI `--dataset-uri`와 lineage 기록

**Files:**
- Modify: `src/cli.py:247-321` (`train_model`), `:348-539` (`run_pipeline`)
- Test: `tests/test_cli.py`

**Interfaces:**
- Consumes: `train.main(dataset_uri=...)` (Task 7), `AssemblyOutcome.snapshot_uri` (Task 5)

- [ ] **Step 1: 실패하는 테스트를 작성한다**

```python
def test_run_pipeline_rejects_dataset_uri_with_events_window(monkeypatch) -> None:
    """스냅샷이 구간을 확정했는데 다른 구간을 받으면 무엇이 진짜인지 답할 수 없다."""
    with pytest.raises(typer.BadParameter, match="dataset-uri"):
        cli.run_pipeline(
            dataset_uri="gs://snapshots/training/by-hash/" + "a" * 64 + "/",
            events_start_date="2026-07-26",
            events_end_date="2026-08-01",
        )


def test_run_pipeline_skips_assembly_and_logs_snapshot_uri(monkeypatch) -> None:
    """--dataset-uri면 조립을 건너뛰고 lineage를 manifest에서 채운다."""
    assembled: list[dict] = []
    monkeypatch.setattr(
        cli.build_training_dataset,
        "main",
        lambda **kwargs: assembled.append(kwargs),
    )
    captured: dict = {}
    monkeypatch.setattr(
        cli.train, "main", lambda **kwargs: captured.update(kwargs) or _OUTCOME_STUB
    )
    monkeypatch.setattr(cli.evaluate, "main", lambda **kwargs: None)

    uri = "gs://snapshots/training/by-hash/" + "a" * 64 + "/"
    cli.run_pipeline(dataset_uri=uri)

    assert assembled == []
    assert captured["dataset_uri"] == uri


def test_run_pipeline_records_snapshot_uri_when_published(monkeypatch) -> None:
    """게시된 실행은 training_snapshot_uri를 MLflow 파라미터에 남긴다."""
    monkeypatch.setattr(
        cli.build_training_dataset,
        "main",
        lambda **kwargs: cli.build_training_dataset.AssemblyOutcome(
            coverage=_COVERAGE_STUB, snapshot_uri="gs://snapshots/by-hash/abc/"
        ),
    )
    captured: dict = {}
    monkeypatch.setattr(
        cli.train, "main", lambda **kwargs: captured.update(kwargs) or _OUTCOME_STUB
    )
    monkeypatch.setattr(cli.evaluate, "main", lambda **kwargs: None)

    cli.run_pipeline(events_start_date="2026-07-26", events_end_date="2026-08-01")

    assert captured["extra_params"]["training_snapshot_uri"] == (
        "gs://snapshots/by-hash/abc/"
    )


def test_run_pipeline_omits_snapshot_uri_when_not_published(monkeypatch) -> None:
    """미게시 실행은 파라미터를 빈 문자열로 넣지 않고 아예 생략한다."""
    monkeypatch.setattr(
        cli.build_training_dataset,
        "main",
        lambda **kwargs: cli.build_training_dataset.AssemblyOutcome(
            coverage=_COVERAGE_STUB, snapshot_uri=None
        ),
    )
    captured: dict = {}
    monkeypatch.setattr(
        cli.train, "main", lambda **kwargs: captured.update(kwargs) or _OUTCOME_STUB
    )
    monkeypatch.setattr(cli.evaluate, "main", lambda **kwargs: None)

    cli.run_pipeline(events_start_date="2026-07-26", events_end_date="2026-08-01")

    assert "training_snapshot_uri" not in captured["extra_params"]
```

두 스텁을 `tests/test_cli.py` 상단에 정의한다. `TrainingOutcome`은
`src/pipeline/train.py:137`의 frozen dataclass이며, `run-pipeline`은 반환값 중
`sampling_rate`와 `pending_registration`만 읽는다:

```python
_OUTCOME_STUB = train.TrainingOutcome(sampling_rate=1.0, run_id="run-stub")

_COVERAGE_STUB = build_training_dataset.SpineCoverage(
    requested_days=("2026-08-01",),
    usable_days=("2026-08-01",),
    sparse_days=(),
    missing_days=(),
    zero_click_days=(),
    total_rows=2,
    total_clicks=1,
)
```

`TrainingOutcome`의 나머지 필드(`registered_version`, `pending_registration`,
`held_out_metric_receipt`, `held_out_metric_receipts`)에 기본값이 없으면 위 생성자에
`None`/빈 튜플로 함께 넘긴다 — 실제 필드 목록은 `train.py:156` 이후를 확인한다.

- [ ] **Step 2: 실패를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_cli.py -k "dataset_uri or snapshot_uri" -q`
Expected: FAIL — `TypeError: run_pipeline() got an unexpected keyword argument 'dataset_uri'`

- [ ] **Step 3: 구현한다**

`train_model`과 `run_pipeline` 양쪽에 옵션을 더한다:

```python
    dataset_uri: Optional[str] = typer.Option(
        None,
        "--dataset-uri",
        help=(
            "게시된 스냅샷 gs://<root>/by-hash/<sha>/ 를 재조립 없이 학습 입력으로 씁니다(#530). "
            "내려받은 뒤 sha·schema·row_count를 재검증하며, 불일치하면 학습 전에 중단합니다."
        ),
    ),
```

`run_pipeline` 본문 맨 앞에 상호배타 검증을 넣는다:

```python
    resolved_dataset_uri = _optional_cli_string(dataset_uri)
    if resolved_dataset_uri is not None:
        conflicting = {
            "--dataset-path": _optional_cli_string(dataset_path),
            "--events-start-date": _optional_cli_string(events_start_date),
            "--events-end-date": _optional_cli_string(events_end_date),
        }
        named = [name for name, value in conflicting.items() if value is not None]
        if named:
            raise typer.BadParameter(
                f"--dataset-uri는 {', '.join(named)}와 함께 쓸 수 없습니다 — "
                "스냅샷이 학습 구간과 입력을 이미 확정했습니다"
            )
```

`train_model`에도 같은 방식으로 `--data-path`와의 배타 검증을 넣는다(옵션 이름만 다르다).

`run_pipeline`의 `[1/4] build-features` 블록을 분기시킨다:

```python
    snapshot_uri: str | None = resolved_dataset_uri
    if resolved_dataset_uri is None:
        typer.echo("\n[1/4] build-features 실행...")
        assembly = build_training_dataset.main(
            output_path=dataset_path,
            events_start_date=events_start_date,
            events_end_date=events_end_date,
            **_coverage_kwargs(min_coverage_days),
            **_assembly_feature_kwargs(
                feature_service=feature_service, extra_features=experiment_features
            ),
            **_snapshot_root_kwargs(snapshot_root),
        )
        coverage = assembly.coverage
        snapshot_uri = assembly.snapshot_uri
    else:
        typer.echo(f"\n[1/4] build-features 생략 — 스냅샷 재사용: {resolved_dataset_uri}")
        coverage = None
```

`data_source_params` 구성을 아래로 바꾼다:

```python
    if resolved_dataset_uri is None:
        data_source_params = {
            "assembly_source": "feast",
            "feature_service": _optional_cli_string(feature_service) or DEFAULT_SERVICE,
            "events_start_date": events_start_date,
            "events_end_date": events_end_date,
            "feast_registry_path": os.environ["GCS_REGISTRY_PATH"],
        }
        requested_min_days = _requested_min_coverage_days(min_coverage_days)
        data_source_params.update(
            coverage.as_lineage_params(
                min_days=(
                    build_training_dataset.DEFAULT_MIN_COVERAGE_DAYS
                    if requested_min_days is None
                    else requested_min_days
                )
            )
        )
    else:
        # 재조립을 하지 않아 조립 반환값이 없다 — sidecar가 같은 값을 전부 갖고 있다(#530).
        data_source_params = {"assembly_source": "snapshot_reuse"}

    # 게시하지 않은 실행에는 이 키를 아예 넣지 않는다 — 빈 문자열로 남기면
    # "게시했는데 URI가 비었다"와 구별되지 않는다(#530 §10-7).
    if snapshot_uri is not None:
        data_source_params["training_snapshot_uri"] = snapshot_uri
```

`train.main(...)` 호출에 두 인자를 더한다:

```python
        dataset_uri=resolved_dataset_uri,
        min_coverage_days=(
            build_training_dataset.DEFAULT_MIN_COVERAGE_DAYS
            if _requested_min_coverage_days(min_coverage_days) is None
            else _requested_min_coverage_days(min_coverage_days)
        ),
```

`min_coverage_days`를 넘기지 않으면 재사용 경로의 커버리지 게이트가 기본값 `0`으로
꺼진 채 돌아, 조립 경로에는 걸리는 하한이 재사용 경로에만 빠지는 비대칭이 생긴다.

`assembly_source="snapshot_reuse"` 외의 lineage 값(`events_*`, `feature_service`,
`feast_registry_path`, `spine_usable_days`)은 **Task 7에서 `train.main`이 직접
채운다** — manifest를 실제로 읽는 주체가 그 함수뿐이라 여기 cli에서는 채울 수 없다.
cli가 넣는 것은 `assembly_source`와 `training_snapshot_uri` 둘뿐이다.

- [ ] **Step 4: 통과를 확인한다**

Run: `$env:PYTHONUTF8="1"; uv run --no-sync python -m pytest tests/test_cli.py -q`
Expected: baseline과 같은 1 failed만 남는다.

- [ ] **Step 5: 커밋**

```bash
git add src/cli.py src/pipeline/train.py tests/test_cli.py
git commit -m "feat: --dataset-uri 재사용 학습과 스냅샷 lineage 기록을 추가한다

Refs #530"
```

---

## Task 9: 문서 갱신

CLAUDE.md Core Rules — 새 필수 환경 변수·공개 CLI 인자를 도입하는 PR은 **같은 PR에서**
`README.md`와 `.claude/docs/agent-project-reference.md`를 갱신한다.

**Files:**
- Modify: `.env.example`, `README.md`, `.claude/docs/agent-project-reference.md`,
  `docs/specs/2026-07-13-public-batch-execution-contract.md`,
  `docs/guides/training-dataset.md`

- [ ] **Step 1: `.env.example`에 변수를 추가한다**

기존 GCS 관련 변수(`GCS_REGISTRY_PATH`, `GCS_STAGING_LOCATION`) 옆에:

```bash
# 학습 데이터셋 스냅샷 게시 루트 (gs://bucket/prefix, #530).
# prod 재학습 경로에만 설정하십시오 — 실험·dev 파이프라인이 설정하면 by-date 포인터가
# 경합합니다. 미설정이면 조립은 로컬에만 저장하고 게시하지 않습니다.
TRAINING_SNAPSHOT_ROOT=
```

- [ ] **Step 2: 공개 batch 계약에 인자를 추가한다**

`docs/specs/2026-07-13-public-batch-execution-contract.md`의 `build-features`,
`train-model`, `run-pipeline` 인자 표에 `--snapshot-root`(build-features, run-pipeline)와
`--dataset-uri`(train-model, run-pipeline)를 더하고, 상호배타 규칙을 한 줄 남긴다.

- [ ] **Step 3: 학습 데이터셋 가이드에 스냅샷 절을 추가한다**

`docs/guides/training-dataset.md` 끝에 `## 💠 스냅샷 게시와 재사용` 절을 추가한다.
최소한 아래를 담는다 — **이 문서 갭이 #530의 최초 문제 제기였다**:

- `training_dataset.csv`가 어디에 저장되는가: 로컬 경로, MLflow
  `reproducibility/snapshot/` artifact, 그리고 게시된 경우 `gs://<root>/by-hash/<sha>/`
- by-date 포인터로 날짜·FeatureService로 찾는 법 (`gsutil cat gs://<root>/by-date/dt=<날짜>/<service>.json`)
- `--dataset-uri`로 재사용하는 법과 그때 수행되는 재검증
- 게시 루트는 prod 재학습 경로에만 설정한다는 운영 계약

- [ ] **Step 4: README와 agent-project-reference를 갱신한다**

두 문서의 환경 변수·공개 CLI 목록에 `TRAINING_SNAPSHOT_ROOT`,
`src/pipeline/training_snapshot_store.py`, 새 CLI 인자를 반영한다.

- [ ] **Step 5: 전체 검증**

```powershell
$env:PYTHONUTF8 = "1"
uv run --no-sync python -m pytest tests/test_training_snapshot_store.py tests/test_build_training_dataset.py tests/test_build_training_dataset_feast_path.py tests/test_pipeline_train.py tests/test_cli.py tests/test_training_provenance.py tests/test_degradation_eval_hold.py tests/test_degradation_eval_detection.py -q
uv run --no-sync ruff check agent_orchestration autoresearch tests tools
git diff --check
```

Expected: baseline과 같은 1 failed(경로 구분자, #531)만 남고 ruff는 clean.

- [ ] **Step 6: 커밋**

```bash
git add .env.example README.md .claude/docs/agent-project-reference.md docs/specs/2026-07-13-public-batch-execution-contract.md docs/guides/training-dataset.md
git commit -m "docs: 학습 데이터셋 스냅샷 게시·재사용 계약을 문서에 반영한다

Refs #530"
```

---

## 완료 후

1. `git push -u origin feat/530-training-dataset-snapshot-store`
2. PR 생성 — 본문에 `Closes #530`, 변경 사항 bullet, 검증 명령 포함
3. spec은 **archive로 옮기지 않는다** — `training_snapshot_store`의 레이아웃·write-once
   의미론은 구현 후에도 살아있는 계약이다 (`docs/README.md`의 "유효한 Spec"에 등재된 상태 유지)
4. `Autoresearch-airflow#236`에 CLI 인자 이름이 확정됐음을 코멘트로 알린다 —
   그쪽 배선이 이름 확정을 기다리고 있다
