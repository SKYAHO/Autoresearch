# Persona Quarantine Collector Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** NVIDIA Persona raw row 정규화 실패 데이터를 버리지 않고 quarantine collector에 저장해 추적, 재처리, 데이터 품질 점검이 가능하게 만든다.

**Architecture:** `source_persona_from_record()`는 지금처럼 `SourcePersona` 변환만 책임지고, 새 loader인 `load_nvidia_persona_records_with_report()`가 valid row와 invalid row를 함께 수집한다. invalid row는 `InvalidPersonaRecord` 모델로 정규화한 뒤 `asset/virtual_user/quarantine/persona_invalid_rows.parquet`와 `asset/virtual_user/quarantine/persona_invalid_summary.json`에 저장한다.

**Tech Stack:** Python 3.13, Pydantic v2, Hugging Face `datasets`, PyArrow Parquet, pytest.

---

## File Structure

- Modify: `autoresearch/virtual_users/schema.py`
  - Add `InvalidPersonaRecord` and `PersonaLoadResult`.
  - Keep `SourcePersona` unchanged as the valid normalized raw-persona contract.

- Modify: `autoresearch/virtual_users/persona_source.py`
  - Add failure classification helpers.
  - Add `load_nvidia_persona_records_with_report()`.
  - Add `write_persona_quarantine_outputs()`.
  - Keep `load_nvidia_persona_records()` backward compatible by returning only `valid_records`.

- Modify: `tests/test_virtual_users_persona_source.py`
  - Add tests for invalid age, invalid sex, missing required fields, quarantine parquet output, summary json output, and backward-compatible valid-only loader behavior.

---

## Task 1: Invalid Persona Schema

**Files:**
- Modify: `autoresearch/virtual_users/schema.py`
- Test: `tests/test_virtual_users_persona_source.py`

- [ ] **Step 1: Write the failing schema test**

Add this test to `tests/test_virtual_users_persona_source.py`:

```python
import json

from autoresearch.virtual_users.schema import InvalidPersonaRecord


def test_invalid_persona_record_preserves_failure_context():
    invalid = InvalidPersonaRecord(
        raw_index=7,
        source_dataset="nvidia/Nemotron-Personas-Korea",
        raw_uuid="raw-007",
        failure_stage="source_persona_normalization",
        failure_reason="invalid_age",
        exception_type="ValueError",
        raw_record_json=json.dumps(
            {"uuid": "raw-007", "age": "twenty", "sex": "female"},
            ensure_ascii=False,
        ),
        created_at="2026-07-02T00:00:00+00:00",
    )

    assert invalid.raw_index == 7
    assert invalid.raw_uuid == "raw-007"
    assert invalid.failure_reason == "invalid_age"
    assert json.loads(invalid.raw_record_json)["age"] == "twenty"
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
python -m pytest tests/test_virtual_users_persona_source.py::test_invalid_persona_record_preserves_failure_context -q
```

Expected: FAIL with `ImportError: cannot import name 'InvalidPersonaRecord'`.

- [ ] **Step 3: Add the schema models**

Add this code to `autoresearch/virtual_users/schema.py` after `SourcePersona`:

```python
class InvalidPersonaRecord(BaseModel):
    raw_index: int
    source_dataset: str
    raw_uuid: str = ""
    failure_stage: str
    failure_reason: str
    exception_type: str
    raw_record_json: str
    created_at: str


class PersonaLoadResult(BaseModel):
    source_dataset: str
    valid_records: list[SourcePersona]
    invalid_records: list[InvalidPersonaRecord]

    @property
    def summary(self) -> dict[str, int]:
        return {
            "valid_count": len(self.valid_records),
            "invalid_count": len(self.invalid_records),
            "total_count": len(self.valid_records) + len(self.invalid_records),
        }
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
python -m pytest tests/test_virtual_users_persona_source.py::test_invalid_persona_record_preserves_failure_context -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/virtual_users/schema.py tests/test_virtual_users_persona_source.py
git commit -m "feat: add invalid persona quarantine schema"
```

---

## Task 2: Loader Report With Valid And Invalid Rows

**Files:**
- Modify: `autoresearch/virtual_users/persona_source.py`
- Test: `tests/test_virtual_users_persona_source.py`

- [ ] **Step 1: Write the failing loader report test**

Add this test to `tests/test_virtual_users_persona_source.py`:

```python
from autoresearch.virtual_users.persona_source import (
    load_persona_records_with_report_from_iterable,
)


def test_load_persona_records_with_report_collects_invalid_rows():
    raw_records = [
        {"uuid": "valid-001", "age": 24, "sex": "female"},
        {"uuid": "bad-age", "age": "twenty", "sex": "male"},
        {"uuid": "bad-sex", "age": 25, "sex": "unknown"},
        {"uuid": "missing-age", "sex": "female"},
    ]

    result = load_persona_records_with_report_from_iterable(raw_records)

    assert result.summary == {
        "valid_count": 1,
        "invalid_count": 3,
        "total_count": 4,
    }
    assert result.valid_records[0].uuid == "valid-001"
    assert [row.failure_reason for row in result.invalid_records] == [
        "invalid_age",
        "invalid_sex",
        "missing_required_field",
    ]
    assert [row.raw_uuid for row in result.invalid_records] == [
        "bad-age",
        "bad-sex",
        "missing-age",
    ]
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
python -m pytest tests/test_virtual_users_persona_source.py::test_load_persona_records_with_report_collects_invalid_rows -q
```

Expected: FAIL with `ImportError: cannot import name 'load_persona_records_with_report_from_iterable'`.

- [ ] **Step 3: Implement failure classification and iterable loader**

Add these imports to `autoresearch/virtual_users/persona_source.py`:

```python
import json
from datetime import UTC, datetime

from autoresearch.virtual_users.schema import (
    SOURCE_DATASET,
    InvalidPersonaRecord,
    PersonaLoadResult,
    SourcePersona,
)
```

Replace the existing single-line import of `SOURCE_DATASET, SourcePersona`.

Add these helpers below `_as_text()`:

```python
def _now_iso() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _failure_reason(record: dict[str, Any], exc: Exception) -> str:
    if isinstance(exc, KeyError):
        return "missing_required_field"
    if "age" in record:
        try:
            int(record["age"])
        except (TypeError, ValueError):
            return "invalid_age"
    if "sex" in record:
        try:
            normalize_sex(record["sex"])
        except ValueError:
            return "invalid_sex"
    return "validation_error"


def _invalid_persona_record(
    raw_index: int,
    record: dict[str, Any],
    exc: Exception,
) -> InvalidPersonaRecord:
    return InvalidPersonaRecord(
        raw_index=raw_index,
        source_dataset=SOURCE_DATASET,
        raw_uuid=_as_text(record, "uuid"),
        failure_stage="source_persona_normalization",
        failure_reason=_failure_reason(record, exc),
        exception_type=type(exc).__name__,
        raw_record_json=json.dumps(record, ensure_ascii=False, default=str),
        created_at=_now_iso(),
    )
```

Add this loader below `source_persona_from_record()`:

```python
def load_persona_records_with_report_from_iterable(
    raw_records: Iterable[dict[str, Any]],
) -> PersonaLoadResult:
    valid_records: list[SourcePersona] = []
    invalid_records: list[InvalidPersonaRecord] = []

    for raw_index, raw_record in enumerate(raw_records):
        record = dict(raw_record)
        try:
            valid_records.append(source_persona_from_record(record))
        except (KeyError, TypeError, ValueError) as exc:
            invalid_records.append(_invalid_persona_record(raw_index, record, exc))

    return PersonaLoadResult(
        source_dataset=SOURCE_DATASET,
        valid_records=valid_records,
        invalid_records=invalid_records,
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
python -m pytest tests/test_virtual_users_persona_source.py::test_load_persona_records_with_report_collects_invalid_rows -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/virtual_users/persona_source.py tests/test_virtual_users_persona_source.py
git commit -m "feat: collect invalid persona rows during loading"
```

---

## Task 3: Quarantine Parquet And Summary Writers

**Files:**
- Modify: `autoresearch/virtual_users/persona_source.py`
- Test: `tests/test_virtual_users_persona_source.py`

- [ ] **Step 1: Write the failing quarantine output test**

Add this test to `tests/test_virtual_users_persona_source.py`:

```python
import json

import pyarrow.parquet as pq

from autoresearch.virtual_users.persona_source import (
    load_persona_records_with_report_from_iterable,
    write_persona_quarantine_outputs,
)


def test_write_persona_quarantine_outputs_writes_parquet_and_summary(tmp_path):
    result = load_persona_records_with_report_from_iterable(
        [
            {"uuid": "valid-001", "age": 24, "sex": "female"},
            {"uuid": "bad-age", "age": "twenty", "sex": "male"},
        ]
    )
    quarantine_dir = tmp_path / "quarantine"

    write_persona_quarantine_outputs(result, quarantine_dir)

    rows = pq.read_table(quarantine_dir / "persona_invalid_rows.parquet").to_pylist()
    summary = json.loads(
        (quarantine_dir / "persona_invalid_summary.json").read_text(encoding="utf-8")
    )

    assert rows[0]["raw_uuid"] == "bad-age"
    assert rows[0]["failure_reason"] == "invalid_age"
    assert summary == {
        "source_dataset": "nvidia/Nemotron-Personas-Korea",
        "valid_count": 1,
        "invalid_count": 1,
        "total_count": 2,
        "failure_reasons": {"invalid_age": 1},
    }
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
python -m pytest tests/test_virtual_users_persona_source.py::test_write_persona_quarantine_outputs_writes_parquet_and_summary -q
```

Expected: FAIL with `ImportError: cannot import name 'write_persona_quarantine_outputs'`.

- [ ] **Step 3: Implement quarantine writers**

Add these imports to `autoresearch/virtual_users/persona_source.py`:

```python
from collections import Counter
from pathlib import Path

import pyarrow as pa
import pyarrow.parquet as pq
```

Add this schema and writer below `load_persona_records_with_report_from_iterable()`:

```python
INVALID_PERSONA_PARQUET_SCHEMA = pa.schema(
    [
        pa.field("raw_index", pa.int64()),
        pa.field("source_dataset", pa.string()),
        pa.field("raw_uuid", pa.string()),
        pa.field("failure_stage", pa.string()),
        pa.field("failure_reason", pa.string()),
        pa.field("exception_type", pa.string()),
        pa.field("raw_record_json", pa.string()),
        pa.field("created_at", pa.string()),
    ]
)


def write_persona_quarantine_outputs(
    result: PersonaLoadResult,
    quarantine_dir: str | Path = "asset/virtual_user/quarantine",
) -> None:
    output_dir = Path(quarantine_dir)
    output_dir.mkdir(parents=True, exist_ok=True)

    rows = [record.model_dump() for record in result.invalid_records]
    table = pa.Table.from_pylist(rows, schema=INVALID_PERSONA_PARQUET_SCHEMA)
    pq.write_table(table, output_dir / "persona_invalid_rows.parquet")

    failure_reasons = Counter(row.failure_reason for row in result.invalid_records)
    summary = {
        "source_dataset": result.source_dataset,
        **result.summary,
        "failure_reasons": dict(sorted(failure_reasons.items())),
    }
    (output_dir / "persona_invalid_summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
```

- [ ] **Step 4: Run the test to verify it passes**

Run:

```bash
python -m pytest tests/test_virtual_users_persona_source.py::test_write_persona_quarantine_outputs_writes_parquet_and_summary -q
```

Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/virtual_users/persona_source.py tests/test_virtual_users_persona_source.py
git commit -m "feat: write invalid persona quarantine outputs"
```

---

## Task 4: Hugging Face Loader Report And Backward Compatibility

**Files:**
- Modify: `autoresearch/virtual_users/persona_source.py`
- Test: `tests/test_virtual_users_persona_source.py`

- [ ] **Step 1: Write the failing compatibility test**

Add this test to `tests/test_virtual_users_persona_source.py`:

```python
from autoresearch.virtual_users.persona_source import (
    load_nvidia_persona_records,
    load_nvidia_persona_records_with_report,
)


def test_load_nvidia_persona_records_returns_only_valid_records(monkeypatch):
    raw_records = [
        {"uuid": "valid-001", "age": 24, "sex": "female"},
        {"uuid": "bad-sex", "age": 24, "sex": "unknown"},
    ]

    def fake_load_dataset(*args, **kwargs):
        return iter(raw_records)

    monkeypatch.setattr(
        "autoresearch.virtual_users.persona_source.load_dataset",
        fake_load_dataset,
    )

    result = load_nvidia_persona_records_with_report(max_records=2)
    valid_records = load_nvidia_persona_records(max_records=2)

    assert result.summary == {
        "valid_count": 1,
        "invalid_count": 1,
        "total_count": 2,
    }
    assert [record.uuid for record in valid_records] == ["valid-001"]
```

- [ ] **Step 2: Run the test to verify it fails**

Run:

```bash
python -m pytest tests/test_virtual_users_persona_source.py::test_load_nvidia_persona_records_returns_only_valid_records -q
```

Expected: FAIL because `load_nvidia_persona_records_with_report` does not exist or `load_dataset` is not monkeypatchable at module scope.

- [ ] **Step 3: Move `load_dataset` import to module scope**

In `autoresearch/virtual_users/persona_source.py`, add:

```python
from datasets import load_dataset
```

Remove the local import inside `load_nvidia_persona_records()`.

- [ ] **Step 4: Add the Hugging Face report loader and preserve old API**

Replace `load_nvidia_persona_records()` with:

```python
def load_nvidia_persona_records_with_report(
    max_records: int | None = None,
) -> PersonaLoadResult:
    logger.info(
        "Loading NVIDIA persona records",
        extra={"source_dataset": SOURCE_DATASET, "max_records": max_records},
    )
    dataset = load_dataset(SOURCE_DATASET, split="train", streaming=True)
    raw_records = []

    for raw_record in dataset:
        raw_records.append(dict(raw_record))
        if max_records is not None and len(raw_records) >= max_records:
            break

    result = load_persona_records_with_report_from_iterable(raw_records)
    logger.info(
        "Loaded NVIDIA persona records",
        extra={
            "source_dataset": SOURCE_DATASET,
            "loaded_count": len(result.valid_records),
            "skipped_count": len(result.invalid_records),
        },
    )
    return result


def load_nvidia_persona_records(max_records: int | None = None) -> list[SourcePersona]:
    return load_nvidia_persona_records_with_report(max_records=max_records).valid_records
```

- [ ] **Step 5: Run the compatibility test to verify it passes**

Run:

```bash
python -m pytest tests/test_virtual_users_persona_source.py::test_load_nvidia_persona_records_returns_only_valid_records -q
```

Expected: PASS.

- [ ] **Step 6: Commit**

```bash
git add autoresearch/virtual_users/persona_source.py tests/test_virtual_users_persona_source.py
git commit -m "feat: expose persona load report with compatibility wrapper"
```

---

## Task 5: Full Verification

**Files:**
- Modify: no production files in this task.
- Test: all tests.

- [ ] **Step 1: Run persona source tests**

Run:

```bash
python -m pytest tests/test_virtual_users_persona_source.py -q
```

Expected: all persona source tests PASS.

- [ ] **Step 2: Run full test suite**

Run:

```bash
python -m pytest -q
```

Expected: all tests PASS.

- [ ] **Step 3: Run ruff**

Run:

```bash
python -m ruff check autoresearch tests
```

Expected: `All checks passed!`

- [ ] **Step 4: Commit verification-only updates if any files changed during cleanup**

If formatting or cleanup changed files:

```bash
git add autoresearch tests
git commit -m "test: verify persona quarantine collector"
```

If no files changed after Task 4, do not create a verification-only commit.

---

## Self-Review

- Spec coverage: The plan covers invalid row schema, failure classification, report loading, quarantine Parquet output, summary JSON output, and old loader compatibility.
- Placeholder scan: The plan contains no unfinished-marker tokens or unspecified implementation sections.
- Type consistency: `InvalidPersonaRecord`, `PersonaLoadResult`, `load_persona_records_with_report_from_iterable()`, `load_nvidia_persona_records_with_report()`, and `write_persona_quarantine_outputs()` use the same names across tests and implementation steps.
