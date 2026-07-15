# Harness 기반 Virtual User 생성 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** raw persona dict를 정규화 없이 하네스로 LLM에 넣어 전체 `VirtualUser` schema를 한 번에 생성하고, 행 단위 장애 격리 + quarantine로 100건 배치를 안전하게 만든다.

**Architecture:** `SourcePersona` 정규화 레이어를 제거하고 raw dict를 그대로 prompt에 넣는다. LLM이 전체 schema를 생성하고, 코드는 id/provenance/meta만 stamp한 뒤 `VirtualUser`로 검증한다. 검증 실패 행은 배치를 중단시키지 않고 quarantine에 격리한다. 새 경로를 먼저 추가하고(green 유지), 파이프라인을 전환한 뒤, 옛 코드를 제거한다.

**Tech Stack:** Python 3.13, Pydantic v2, PyArrow Parquet, OpenAI-compatible GLM client, Hugging Face `datasets`, pytest.

기준 설계 문서: `docs/archive/specs/harness-기반-virtual-user-생성-설계.md`

## Global Constraints

- 출력 `VirtualUser` / parquet / warehouse JSONL schema는 기존과 동일하게 유지한다 (필드 변경 없음).
- LLM은 전체 schema를 생성한다. 코드가 stamp하는 값: `virtual_user_id`, `source_uuid`, `source_hash`, `source_persona_json`, `age_bucket`, `generation_meta`.
- 한 행 실패가 배치를 중단시키면 안 된다 (fault isolation).
- 실패 행은 `quarantine.jsonl`에 `{source_uuid, raw_row, raw_llm_response, error_type, error_message}`로 남긴다. `error_type ∈ {api_error, invalid_json, schema_fail}`.
- 균형 샘플러(연령대·성별, seed 고정)는 유지하되 raw dict 기반으로 동작한다.
- category 값은 `DEFAULT_KAGGLE_YOUTUBE_CATEGORIES` vocabulary 안에서만 허용한다.
- 검증 명령: `python -m pytest -q`.

---

## File Structure

- `autoresearch/virtual_users/schema.py`
  - 유지: `GenerationRequest`, `YouTubeProfile`, `GenerationMeta`, `VirtualUser`, `VirtualUserBatch`, `age_bucket_for_age`, 상수.
  - 추가: `QuarantineRecord`, `GenerationResult`. `GenerationRequest.quarantine_output_path`.
  - 제거(Task 6): `SourcePersona`, `DerivedVirtualUserFeatures`.
- `autoresearch/virtual_users/persona_source.py`
  - 추가: `load_raw_persona_records`, `sample_raw_personas_by_contract`, `build_fixture_raw_persona_records`, `record_age`, `record_sex`.
  - 유지: `normalize_sex`, `write_raw_persona_records`.
  - 제거(Task 6): `source_persona_from_record`, `sample_personas_by_contract`, `build_fixture_persona_records`, `load_nvidia_persona_records`, `_as_text*`, `_source_text`, `_source_hash`.
- `autoresearch/virtual_users/glm_generator.py`
  - 추가: `build_source_hash`, `build_virtual_user_prompt`(raw dict용, 기존 함수 대체), `assemble_virtual_user`.
  - 변경: `RuleBasedVirtualUserGenerator.generate` / `GLMVirtualUserGenerator.generate` 가 raw dict를 받아 **raw JSON text**를 반환.
  - 제거(Task 6): `_virtual_user_from_derived_features`, `parse_virtual_user_json`(derived 전용), `interests` 의존.
- `autoresearch/virtual_users/pipeline.py`
  - 변경: `generate_virtual_user_batch(request, records: list[dict], generator) -> GenerationResult`, 행 단위 격리, quarantine 출력.
  - 추가: `write_quarantine_jsonl`.
- `autoresearch/virtual_users/categories.py` — 변경 없음 (vocabulary는 prompt에서 계속 사용).
- `autoresearch/virtual_users/interests.py` — Task 6에서 rule-based generator가 더 이상 사용하지 않으면 파일 정리.

---

## Task 1: Raw dict 균형 샘플러

**Files:**
- Modify: `autoresearch/virtual_users/persona_source.py`
- Test: `tests/test_virtual_users_persona_source.py`

**Interfaces:**
- Produces:
  - `record_age(record: dict) -> int | None`
  - `record_sex(record: dict) -> str | None`  (`"male"|"female"|None`)
  - `sample_raw_personas_by_contract(records: list[dict], age_min: int, age_max: int, male_count: int, female_count: int, seed: int) -> list[dict]`

- [ ] **Step 1: Write the failing test**

`tests/test_virtual_users_persona_source.py`에 추가:

```python
from autoresearch.virtual_users.persona_source import (
    record_age,
    record_sex,
    sample_raw_personas_by_contract,
)


def _raw_rows():
    rows = []
    for i in range(60):
        rows.append({"uuid": f"m-{i}", "age": 20 + (i % 10), "sex": "남자"})
    for i in range(60):
        rows.append({"uuid": f"f-{i}", "age": 20 + (i % 10), "sex": "여자"})
    return rows


def test_record_age_and_sex_read_from_raw_dict():
    assert record_age({"age": "25"}) == 25
    assert record_age({"age": None}) is None
    assert record_sex({"sex": "여자"}) == "female"
    assert record_sex({"sex": "unknown"}) is None


def test_sample_raw_personas_by_contract_returns_balanced_seeded_sample():
    rows = _raw_rows()

    sample = sample_raw_personas_by_contract(
        records=rows, age_min=20, age_max=29, male_count=50, female_count=50, seed=42
    )
    again = sample_raw_personas_by_contract(
        records=rows, age_min=20, age_max=29, male_count=50, female_count=50, seed=42
    )

    assert len(sample) == 100
    assert sum(1 for r in sample if record_sex(r) == "male") == 50
    assert sum(1 for r in sample if record_sex(r) == "female") == 50
    assert [r["uuid"] for r in sample] == [r["uuid"] for r in again]  # deterministic
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_users_persona_source.py::test_sample_raw_personas_by_contract_returns_balanced_seeded_sample -q`
Expected: FAIL with `ImportError: cannot import name 'sample_raw_personas_by_contract'`.

- [ ] **Step 3: Implement helpers and sampler**

`autoresearch/virtual_users/persona_source.py`에 추가 (기존 `normalize_sex` 아래):

```python
def record_age(record: dict[str, Any]) -> int | None:
    """raw record의 age를 int로 읽되, 불가하면 None."""
    try:
        return int(record["age"])
    except (KeyError, TypeError, ValueError):
        return None


def record_sex(record: dict[str, Any]) -> str | None:
    """raw record의 sex를 male/female로 읽되, 불가하면 None."""
    try:
        return normalize_sex(record["sex"])
    except (KeyError, ValueError):
        return None


def sample_raw_personas_by_contract(
    records: list[dict[str, Any]],
    age_min: int,
    age_max: int,
    male_count: int,
    female_count: int,
    seed: int,
) -> list[dict[str, Any]]:
    """raw dict에서 연령/성별을 읽어 seed 기반 균형 샘플을 만든다."""
    eligible = [
        record
        for record in records
        if (age := record_age(record)) is not None and age_min <= age <= age_max
    ]
    male_records = [r for r in eligible if record_sex(r) == "male"]
    female_records = [r for r in eligible if record_sex(r) == "female"]

    if len(male_records) < male_count:
        raise ValueError(
            f"Not enough male personas: requested={male_count}, available={len(male_records)}"
        )
    if len(female_records) < female_count:
        raise ValueError(
            f"Not enough female personas: requested={female_count}, "
            f"available={len(female_records)}"
        )

    rng = random.Random(seed)
    male_pool = list(male_records)
    female_pool = list(female_records)
    rng.shuffle(male_pool)
    rng.shuffle(female_pool)
    sampled = male_pool[:male_count] + female_pool[:female_count]
    rng.shuffle(sampled)

    logger.info(
        "Sampled raw personas for virtual user generation",
        extra={
            "sampled_total": len(sampled),
            "sampled_male_count": male_count,
            "sampled_female_count": female_count,
            "seed": seed,
        },
    )
    return sampled
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_virtual_users_persona_source.py::test_record_age_and_sex_read_from_raw_dict tests/test_virtual_users_persona_source.py::test_sample_raw_personas_by_contract_returns_balanced_seeded_sample -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/virtual_users/persona_source.py tests/test_virtual_users_persona_source.py
git commit -m "feat: add raw-dict balanced persona sampler"
```

---

## Task 2: Raw persona 로더와 fixture (dict 기반)

**Files:**
- Modify: `autoresearch/virtual_users/persona_source.py`
- Test: `tests/test_virtual_users_persona_source.py`

**Interfaces:**
- Consumes: `write_raw_persona_records` (기존).
- Produces:
  - `build_fixture_raw_persona_records(male_count: int = 60, female_count: int = 60) -> list[dict]`
  - `load_raw_persona_records(max_records: int | None = None, raw_output_path: str | Path | None = None) -> list[dict]`

- [ ] **Step 1: Write the failing test**

`tests/test_virtual_users_persona_source.py`에 추가:

```python
from autoresearch.virtual_users.persona_source import (
    build_fixture_raw_persona_records,
    load_raw_persona_records,
)


def test_build_fixture_raw_persona_records_returns_balanced_raw_dicts():
    rows = build_fixture_raw_persona_records(male_count=3, female_count=2)

    assert len(rows) == 5
    assert all(isinstance(r, dict) for r in rows)
    assert sum(1 for r in rows if r["sex"] == "남자") == 3
    assert sum(1 for r in rows if r["sex"] == "여자") == 2
    assert rows[0]["uuid"]
    assert rows[0]["persona"]


def test_load_raw_persona_records_returns_raw_dicts_and_snapshot(monkeypatch, tmp_path):
    fake = [{"uuid": "a", "age": 24, "sex": "여자", "persona": "p"}]

    def fake_load_dataset(*args, **kwargs):
        return iter(fake)

    monkeypatch.setattr(
        "autoresearch.virtual_users.persona_source.load_dataset", fake_load_dataset
    )
    snapshot = tmp_path / "raw.jsonl"

    rows = load_raw_persona_records(max_records=1, raw_output_path=snapshot)

    assert rows == fake
    assert snapshot.read_text(encoding="utf-8").strip()
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_users_persona_source.py::test_build_fixture_raw_persona_records_returns_balanced_raw_dicts tests/test_virtual_users_persona_source.py::test_load_raw_persona_records_returns_raw_dicts_and_snapshot -q`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement loaders**

`autoresearch/virtual_users/persona_source.py`에 추가:

```python
def build_fixture_raw_persona_records(
    male_count: int = 60,
    female_count: int = 60,
) -> list[dict[str, Any]]:
    """외부 dataset/LLM 없이 테스트할 수 있는 deterministic raw dict fixture."""
    rows: list[dict[str, Any]] = []
    for index in range(male_count):
        rows.append(
            {
                "uuid": f"fixture-m-{index:03d}",
                "age": 20 + (index % 10),
                "sex": "남자",
                "occupation": "student" if index % 2 == 0 else "office worker",
                "province": "서울",
                "district": "마포구",
                "persona": "게임과 음악을 즐기는 20대 남성.",
                "hobbies_and_interests": "게임, 음악, 숏폼",
            }
        )
    for index in range(female_count):
        rows.append(
            {
                "uuid": f"fixture-f-{index:03d}",
                "age": 20 + (index % 10),
                "sex": "여자",
                "occupation": "student" if index % 2 == 0 else "designer",
                "province": "경기",
                "district": "성남시",
                "persona": "음악과 라이프스타일을 즐기는 20대 여성.",
                "hobbies_and_interests": "음악, 뷰티, 라이프스타일",
            }
        )
    return rows


def load_raw_persona_records(
    max_records: int | None = None,
    raw_output_path: str | Path | None = None,
) -> list[dict[str, Any]]:
    """NVIDIA Persona dataset을 streaming으로 읽어 raw dict 그대로 반환한다."""
    logger.info(
        "Loading raw NVIDIA persona records",
        extra={"source_dataset": SOURCE_DATASET, "max_records": max_records},
    )
    dataset = load_dataset(SOURCE_DATASET, split="train", streaming=True)
    records: list[dict[str, Any]] = []
    for raw_record in dataset:
        records.append(dict(raw_record))
        if max_records is not None and len(records) >= max_records:
            break

    if raw_output_path is not None:
        write_raw_persona_records(records, raw_output_path)

    logger.info(
        "Loaded raw NVIDIA persona records",
        extra={"source_dataset": SOURCE_DATASET, "loaded_count": len(records)},
    )
    return records
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_virtual_users_persona_source.py -q -k "raw"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/virtual_users/persona_source.py tests/test_virtual_users_persona_source.py
git commit -m "feat: load raw persona dicts without normalization"
```

---

## Task 3: Quarantine / Result schema

**Files:**
- Modify: `autoresearch/virtual_users/schema.py`
- Test: `tests/test_virtual_users_schema.py`

**Interfaces:**
- Consumes: `VirtualUser`, `VirtualUserBatch` (기존).
- Produces:
  - `QuarantineRecord(source_uuid: str, raw_row: dict, raw_llm_response: str, error_type: Literal["api_error","invalid_json","schema_fail"], error_message: str)`
  - `GenerationResult(batch: VirtualUserBatch, quarantine: list[QuarantineRecord])` with `.summary -> dict[str,int]`
  - `GenerationRequest.quarantine_output_path: str`

- [ ] **Step 1: Write the failing test**

`tests/test_virtual_users_schema.py`에 추가:

```python
from autoresearch.virtual_users.schema import GenerationResult, QuarantineRecord


def test_quarantine_record_captures_failure_context():
    record = QuarantineRecord(
        source_uuid="p-1",
        raw_row={"uuid": "p-1", "sex": "여자"},
        raw_llm_response="{bad json",
        error_type="invalid_json",
        error_message="Expecting value",
    )
    assert record.error_type == "invalid_json"
    assert record.raw_row["sex"] == "여자"


def test_generation_result_summary_counts_valid_and_quarantine():
    result = GenerationResult(
        batch=_make_single_user_batch(),  # 기존 헬퍼 또는 아래 fixture 재사용
        quarantine=[
            QuarantineRecord(
                source_uuid="p-2",
                raw_row={"uuid": "p-2"},
                raw_llm_response="",
                error_type="api_error",
                error_message="timeout",
            )
        ],
    )
    assert result.summary == {"valid": 1, "quarantined": 1, "api_error": 1,
                              "invalid_json": 0, "schema_fail": 0}
```

> `_make_single_user_batch()`는 이 파일의 기존 `VirtualUser`/`VirtualUserBatch` fixture 구성 코드를 재사용해 1명짜리 batch를 만드는 헬퍼다. 기존 `test_virtual_user_batch_counts_users_by_sex`의 batch 생성 부분을 헬퍼로 추출해 사용한다.

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_users_schema.py::test_quarantine_record_captures_failure_context -q`
Expected: FAIL with `ImportError: cannot import name 'QuarantineRecord'`.

- [ ] **Step 3: Add models**

`autoresearch/virtual_users/schema.py`의 `VirtualUserBatch` 아래에 추가:

```python
class QuarantineRecord(BaseModel):
    """생성 실패로 격리된 행. 후처리를 위해 원본과 raw 응답을 보존한다."""

    source_uuid: str = ""
    raw_row: dict[str, object] = Field(default_factory=dict)
    raw_llm_response: str = ""
    error_type: Literal["api_error", "invalid_json", "schema_fail"]
    error_message: str = ""


class GenerationResult(BaseModel):
    """유효 batch와 격리 행을 함께 담는 배치 실행 결과."""

    batch: "VirtualUserBatch"
    quarantine: list[QuarantineRecord] = Field(default_factory=list)

    @property
    def summary(self) -> dict[str, int]:
        counts = {"api_error": 0, "invalid_json": 0, "schema_fail": 0}
        for record in self.quarantine:
            counts[record.error_type] += 1
        return {
            "valid": len(self.batch.users),
            "quarantined": len(self.quarantine),
            **counts,
        }
```

`GenerationRequest`에 필드 추가:

```python
    quarantine_output_path: str = "data/generated/virtual_users_quarantine.jsonl"
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_virtual_users_schema.py -q -k "quarantine or generation_result"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/virtual_users/schema.py tests/test_virtual_users_schema.py
git commit -m "feat: add quarantine and generation result schema"
```

---

## Task 4: Prompt 하네스 + row 조립/검증

**Files:**
- Modify: `autoresearch/virtual_users/glm_generator.py`
- Test: `tests/test_virtual_users_glm_generator.py`

**Interfaces:**
- Consumes: `VirtualUser`, `age_bucket_for_age`, `SOURCE_COUNTRY`, `SOURCE_LOCALE`, `GENERATION_SCHEMA_VERSION`, `PROMPT_VERSION`, `DEFAULT_KAGGLE_YOUTUBE_CATEGORIES`.
- Produces:
  - `build_source_hash(record: dict) -> str`
  - `build_virtual_user_prompt(raw_row: dict, virtual_user_id: str) -> str`
  - `assemble_virtual_user(raw_row: dict, raw_text: str, virtual_user_id: str, model_name: str) -> VirtualUser`
    - `json.JSONDecodeError` (invalid_json) 또는 `pydantic.ValidationError`/`ValueError` (schema_fail)를 raise 할 수 있다.

- [ ] **Step 1: Write the failing test**

`tests/test_virtual_users_glm_generator.py`에 추가. `_full_content()`는 LLM이 낼 전체 content JSON을 흉내낸다.

```python
import json

import pytest

from autoresearch.virtual_users.glm_generator import (
    assemble_virtual_user,
    build_source_hash,
    build_virtual_user_prompt,
)
from autoresearch.virtual_users.schema import VirtualUser


def _raw_row():
    return {"uuid": "p-001", "age": 24, "sex": "여자", "persona": "제주 서점 직원"}


def _full_content():
    return {
        "age": 24,
        "sex": "female",
        "occupation": "판매원",
        "province": "제주",
        "district": "제주시",
        "marital_status": "미혼",
        "military_status": "비현역",
        "family_type": "1인 가구",
        "housing_type": "아파트",
        "education_level": "4년제 대학교",
        "bachelors_field": "교육",
        "persona_summary": "제주의 20대 여성.",
        "hobby_keywords": ["독서"],
        "interest_keywords": ["음악"],
        "lifestyle_keywords": [],
        "food_keywords": [],
        "travel_keywords": [],
        "career_keywords": [],
        "family_context_keywords": [],
        "category_evidence": {"Music": ["LP"]},
        "category_affinity": {"Music": 0.8},
        "youtube_profile": {
            "primary_categories": ["Music"],
            "shorts_affinity": 0.6,
            "longform_affinity": 0.5,
            "trend_sensitivity": 0.4,
            "comment_propensity": 0.2,
            "watch_time_band": "night",
        },
    }


def test_build_virtual_user_prompt_embeds_raw_row_and_vocab():
    prompt = build_virtual_user_prompt(_raw_row(), "vu_0001")
    assert "p-001" in prompt          # raw row 포함
    assert "제주 서점 직원" in prompt
    assert "Music" in prompt          # allowed vocabulary
    assert "sex_normalized" not in prompt


def test_assemble_virtual_user_stamps_code_owned_fields():
    user = assemble_virtual_user(
        raw_row=_raw_row(),
        raw_text=json.dumps(_full_content(), ensure_ascii=False),
        virtual_user_id="vu_0001",
        model_name="glm-5.2",
    )
    assert isinstance(user, VirtualUser)
    assert user.virtual_user_id == "vu_0001"
    assert user.source_uuid == "p-001"                 # code-stamped from raw row
    assert user.source_hash == build_source_hash(_raw_row())
    assert user.age_bucket == "20s"                    # code-computed from age
    assert user.source_persona_json == _raw_row()      # raw row preserved
    assert user.generation_meta.llm_model == "glm-5.2"
    assert user.sex == "female"                         # LLM content


def test_assemble_virtual_user_raises_on_invalid_json():
    with pytest.raises(json.JSONDecodeError):
        assemble_virtual_user(_raw_row(), "{not json", "vu_0001", "glm-5.2")


def test_assemble_virtual_user_raises_on_schema_violation():
    from pydantic import ValidationError

    bad = _full_content()
    bad["youtube_profile"]["shorts_affinity"] = 5.0  # out of range
    with pytest.raises(ValidationError):
        assemble_virtual_user(
            _raw_row(), json.dumps(bad), "vu_0001", "glm-5.2"
        )
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_users_glm_generator.py -q -k "assemble or embeds_raw"`
Expected: FAIL with `ImportError`.

- [ ] **Step 3: Implement prompt + assemble**

`autoresearch/virtual_users/glm_generator.py` 상단 import 정리 후 추가:

```python
import hashlib

from pydantic import ValidationError  # noqa: F401  (호출부에서 사용)

from autoresearch.virtual_users.categories import DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_COUNTRY,
    SOURCE_LOCALE,
    VirtualUser,
    age_bucket_for_age,
)


def build_source_hash(record: dict) -> str:
    """raw row의 안정적 추적 hash."""
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


GLM_SYSTEM_HARNESS = """너는 virtual user row generator다.
아래 원본 persona를 근거로 지정된 JSON schema를 채운다.
없는 정보를 지어내지 마라. 원본에서 추론 가능한 수준만 생성하라.
category 값은 제공된 allowed category vocabulary 안에서만 선택하라.
virtual_user_id, source_uuid, source_hash, source_persona_json, age_bucket, generation_meta는 만들지 마라(코드가 채운다).
출력은 지정된 JSON 하나만 허용한다. Markdown이나 주석을 넣지 마라.
"""


def build_virtual_user_prompt(raw_row: dict, virtual_user_id: str) -> str:
    """raw persona dict 전체와 허용 vocab을 GLM user prompt로 만든다."""
    allowed = "\n".join(f"- {c}" for c in DEFAULT_KAGGLE_YOUTUBE_CATEGORIES)
    prompt = f"""You convert a Korean persona into a virtual YouTube user row.

Prompt version: {PROMPT_VERSION}
Schema version: {GENERATION_SCHEMA_VERSION}

Source persona (raw):
{json.dumps(raw_row, ensure_ascii=False, indent=2)}

Allowed category vocabulary:
{allowed}

Return only JSON with this shape (no Markdown, no commentary):
{{
  "age": 24, "sex": "female",
  "occupation": "", "province": "", "district": "",
  "marital_status": "", "military_status": "", "family_type": "",
  "housing_type": "", "education_level": "", "bachelors_field": "",
  "persona_summary": "one sentence",
  "hobby_keywords": [], "interest_keywords": [], "lifestyle_keywords": [],
  "food_keywords": [], "travel_keywords": [], "career_keywords": [],
  "family_context_keywords": [],
  "category_evidence": {{"Music": ["short grounded phrase"]}},
  "category_affinity": {{"Music": 0.8}},
  "youtube_profile": {{
    "primary_categories": ["Music"],
    "shorts_affinity": 0.0, "longform_affinity": 0.0,
    "trend_sensitivity": 0.0, "comment_propensity": 0.0,
    "watch_time_band": "night"
  }}
}}

Constraints:
- sex must be "male" or "female".
- All affinity numbers between 0 and 1.
- primary_categories: 1 to 5 items from the allowed vocabulary.
- category_evidence / category_affinity keys must be from the allowed vocabulary.
- watch_time_band in [morning, afternoon, evening, night, mixed].
"""
    logger.debug(
        "Built virtual user prompt",
        extra={"virtual_user_id": virtual_user_id, "prompt_version": PROMPT_VERSION},
    )
    return prompt


def assemble_virtual_user(
    raw_row: dict,
    raw_text: str,
    virtual_user_id: str,
    model_name: str,
) -> VirtualUser:
    """LLM content를 parse하고 코드 stamp 필드를 얹어 VirtualUser로 검증한다."""
    payload = json.loads(raw_text)  # raises json.JSONDecodeError -> invalid_json

    payload["virtual_user_id"] = virtual_user_id
    payload["source_uuid"] = str(raw_row.get("uuid", ""))
    payload["source_hash"] = build_source_hash(raw_row)
    payload["source_persona_json"] = raw_row
    payload["age_bucket"] = age_bucket_for_age(int(payload["age"]))
    payload.setdefault("country", SOURCE_COUNTRY)
    payload.setdefault("locale", SOURCE_LOCALE)
    payload["generation_meta"] = {
        "schema_version": GENERATION_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
        "llm_model": model_name,
        "generated_at": _now_iso(),
    }
    return VirtualUser.model_validate(payload)  # raises ValidationError -> schema_fail
```

> `_now_iso()`는 기존 파일에 이미 있다. 기존 `build_virtual_user_prompt`(SourcePersona용)와 `parse_virtual_user_json`, `_virtual_user_from_derived_features`는 Task 6에서 제거한다. 이 태스크에서는 새 함수를 추가만 한다(이름 충돌 시 기존 `build_virtual_user_prompt`를 새 시그니처로 교체).

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_virtual_users_glm_generator.py -q -k "assemble or embeds_raw"`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/virtual_users/glm_generator.py tests/test_virtual_users_glm_generator.py
git commit -m "feat: add raw-dict prompt harness and virtual user assembly"
```

---

## Task 5: Generator가 raw dict를 받아 raw text 반환

**Files:**
- Modify: `autoresearch/virtual_users/glm_generator.py`
- Test: `tests/test_virtual_users_glm_generator.py`

**Interfaces:**
- Produces:
  - `RuleBasedVirtualUserGenerator.generate(raw_row: dict, virtual_user_id: str) -> str` (전체 content JSON string)
  - `GLMVirtualUserGenerator.generate(raw_row: dict, virtual_user_id: str) -> str` (LLM raw text)
  - 둘 다 `.model_name: str` 속성을 갖는다.

- [ ] **Step 1: Write the failing test**

```python
from autoresearch.virtual_users.glm_generator import RuleBasedVirtualUserGenerator
from autoresearch.virtual_users.glm_generator import assemble_virtual_user


def test_rule_based_generator_returns_assemblable_full_content():
    gen = RuleBasedVirtualUserGenerator()
    raw = {"uuid": "p-9", "age": 24, "sex": "여자", "persona": "게임을 좋아함",
           "hobbies_and_interests": "게임, 음악"}

    raw_text = gen.generate(raw, "vu_0001")
    user = assemble_virtual_user(raw, raw_text, "vu_0001", gen.model_name)

    assert user.sex == "female"
    assert user.youtube_profile.primary_categories
    assert user.category_affinity
    assert "sex_normalized" not in user.source_persona_json
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_users_glm_generator.py::test_rule_based_generator_returns_assemblable_full_content -q`
Expected: FAIL (기존 generate가 SourcePersona/VirtualUser 시그니처라 dict 입력에서 깨짐).

- [ ] **Step 3: Rewrite generators**

`RuleBasedVirtualUserGenerator`를 아래로 교체:

```python
class RuleBasedVirtualUserGenerator:
    """LLM 없이 전체 content JSON을 만드는 deterministic fixture generator."""

    def __init__(self, model_name: str = "fixture-rule-generator") -> None:
        self.model_name = model_name

    def generate(self, raw_row: dict, virtual_user_id: str) -> str:
        text = " ".join(
            str(raw_row.get(k, ""))
            for k in ("persona", "hobbies_and_interests", "occupation")
        ).lower()
        if "game" in text or "게임" in text:
            categories, band = ["Gaming", "Music"], "night"
        elif "study" in text or "학습" in text:
            categories, band = ["Education", "Science & Technology"], "evening"
        else:
            categories, band = ["Music", "Entertainment"], "mixed"

        content = {
            "age": int(raw_row.get("age", 20)),
            "sex": record_sex(raw_row) or "male",
            "occupation": str(raw_row.get("occupation", "")),
            "province": str(raw_row.get("province", "")),
            "district": str(raw_row.get("district", "")),
            "marital_status": str(raw_row.get("marital_status", "")),
            "military_status": str(raw_row.get("military_status", "")),
            "family_type": str(raw_row.get("family_type", "")),
            "housing_type": str(raw_row.get("housing_type", "")),
            "education_level": str(raw_row.get("education_level", "")),
            "bachelors_field": str(raw_row.get("bachelors_field", "")),
            "persona_summary": str(raw_row.get("persona", ""))[:180] or "20s KR user.",
            "hobby_keywords": [], "interest_keywords": [], "lifestyle_keywords": [],
            "food_keywords": [], "travel_keywords": [], "career_keywords": [],
            "family_context_keywords": [],
            "category_evidence": {c: ["fixture"] for c in categories},
            "category_affinity": {c: round(0.8 - 0.1 * i, 2) for i, c in enumerate(categories)},
            "youtube_profile": {
                "primary_categories": categories,
                "shorts_affinity": 0.68, "longform_affinity": 0.51,
                "trend_sensitivity": 0.61, "comment_propensity": 0.25,
                "watch_time_band": band,
            },
        }
        return json.dumps(content, ensure_ascii=False)
```

> `record_sex`를 `persona_source`에서 import 한다: `from autoresearch.virtual_users.persona_source import record_sex`.

`GLMVirtualUserGenerator.generate`를 raw text 반환으로 변경:

```python
    def generate(self, raw_row: dict, virtual_user_id: str) -> str:
        from openai import OpenAI

        client = OpenAI(**self._client_kwargs())
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": GLM_SYSTEM_HARNESS},
                {"role": "user", "content": build_virtual_user_prompt(raw_row, virtual_user_id)},
            ],
            response_format={"type": "json_object"},
        )
        return response.choices[0].message.content or ""
```

- [ ] **Step 4: Run test to verify it passes**

Run: `python -m pytest tests/test_virtual_users_glm_generator.py::test_rule_based_generator_returns_assemblable_full_content -q`
Expected: PASS.

- [ ] **Step 5: Commit**

```bash
git add autoresearch/virtual_users/glm_generator.py tests/test_virtual_users_glm_generator.py
git commit -m "feat: generators emit full-schema raw text from raw dict"
```

---

## Task 6: Pipeline 장애 격리 + quarantine

**Files:**
- Modify: `autoresearch/virtual_users/pipeline.py`
- Test: `tests/test_virtual_users_pipeline.py`

**Interfaces:**
- Consumes: `sample_raw_personas_by_contract`, `assemble_virtual_user`, generator `.generate(raw_row, vid)->str` + `.model_name`, `QuarantineRecord`, `GenerationResult`.
- Produces:
  - `generate_virtual_user_batch(request, records: list[dict], generator) -> GenerationResult`
  - `write_quarantine_jsonl(records: list[QuarantineRecord], output_path: str | Path) -> None`

- [ ] **Step 1: Write the failing test (fault isolation regression)**

```python
import json

from autoresearch.virtual_users.glm_generator import RuleBasedVirtualUserGenerator
from autoresearch.virtual_users.persona_source import build_fixture_raw_persona_records
from autoresearch.virtual_users.pipeline import generate_virtual_user_batch
from autoresearch.virtual_users.schema import GenerationRequest


class _OneBadGenerator(RuleBasedVirtualUserGenerator):
    """두 번째 호출에서만 잘못된 JSON을 반환한다."""

    def __init__(self):
        super().__init__()
        self._n = 0

    def generate(self, raw_row, virtual_user_id):
        self._n += 1
        if self._n == 2:
            return "{not valid json"
        return super().generate(raw_row, virtual_user_id)


def test_batch_isolates_bad_row_and_quarantines_it(tmp_path):
    records = build_fixture_raw_persona_records(male_count=2, female_count=1)
    request = GenerationRequest(
        male_count=2, female_count=1, use_llm=False,
        output_path=str(tmp_path / "vu.parquet"),
        warehouse_output_path=str(tmp_path / "vu.jsonl"),
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )

    result = generate_virtual_user_batch(request, records, _OneBadGenerator())

    assert result.summary["valid"] == 2          # 배치가 안 죽음
    assert result.summary["quarantined"] == 1
    assert result.summary["invalid_json"] == 1
    q_lines = (tmp_path / "q.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(q_lines) == 1
    assert json.loads(q_lines[0])["error_type"] == "invalid_json"
```

- [ ] **Step 2: Run test to verify it fails**

Run: `python -m pytest tests/test_virtual_users_pipeline.py::test_batch_isolates_bad_row_and_quarantines_it -q`
Expected: FAIL (현재 pipeline은 SourcePersona 기반이며 예외 시 배치 중단).

- [ ] **Step 3: Rewrite pipeline core**

`pipeline.py`에서 import를 아래로 교체하고 `sample_personas_by_contract` → `sample_raw_personas_by_contract`, generator 호출을 격리 구조로 바꾼다.

```python
import json
from pathlib import Path

from pydantic import ValidationError

from autoresearch.virtual_users.glm_generator import assemble_virtual_user
from autoresearch.virtual_users.persona_source import sample_raw_personas_by_contract
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_DATASET,
    GenerationRequest,
    GenerationResult,
    QuarantineRecord,
    VirtualUser,
    VirtualUserBatch,
)


def write_quarantine_jsonl(records: list[QuarantineRecord], output_path: str | Path) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for record in records:
            file.write(json.dumps(record.model_dump(), ensure_ascii=False, default=str) + "\n")


def _generate_isolated(
    generator, records: list[dict]
) -> tuple[list[VirtualUser], list[QuarantineRecord]]:
    users: list[VirtualUser] = []
    quarantine: list[QuarantineRecord] = []
    for index, raw_row in enumerate(records, start=1):
        vid = f"vu_{index:04d}"
        source_uuid = str(raw_row.get("uuid", ""))
        try:
            raw_text = generator.generate(raw_row, vid)
        except Exception as exc:  # noqa: BLE001 - API/transport failure isolation
            quarantine.append(QuarantineRecord(
                source_uuid=source_uuid, raw_row=raw_row, raw_llm_response="",
                error_type="api_error", error_message=str(exc)))
            continue
        try:
            users.append(assemble_virtual_user(raw_row, raw_text, vid, generator.model_name))
        except json.JSONDecodeError as exc:
            quarantine.append(QuarantineRecord(
                source_uuid=source_uuid, raw_row=raw_row, raw_llm_response=raw_text,
                error_type="invalid_json", error_message=str(exc)))
        except (ValidationError, ValueError, KeyError) as exc:
            quarantine.append(QuarantineRecord(
                source_uuid=source_uuid, raw_row=raw_row, raw_llm_response=raw_text,
                error_type="schema_fail", error_message=str(exc)))
    return users, quarantine
```

`generate_virtual_user_batch`를 아래로 교체:

```python
def generate_virtual_user_batch(
    request: GenerationRequest,
    records: list[dict],
    generator,
) -> GenerationResult:
    sampled = sample_raw_personas_by_contract(
        records=records,
        age_min=request.age_min, age_max=request.age_max,
        male_count=request.male_count, female_count=request.female_count,
        seed=request.seed,
    )
    users, quarantine = _generate_isolated(generator, sampled)

    batch = VirtualUserBatch(
        schema_version=GENERATION_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        source_dataset=SOURCE_DATASET,
        request=request,
        users=users,
    )
    result = GenerationResult(batch=batch, quarantine=quarantine)
    logger.info("Generated virtual user batch", extra=result.summary)

    output_path = Path(request.output_path)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    _write_virtual_users_parquet(batch, output_path)
    write_virtual_users_warehouse_jsonl(batch, request.warehouse_output_path)
    write_quarantine_jsonl(quarantine, request.quarantine_output_path)
    return result
```

> `_write_virtual_users_parquet`, `write_virtual_users_warehouse_jsonl`, `VIRTUAL_USERS_PARQUET_SCHEMA`는 그대로 재사용한다. 병렬 생성(`_generate_users`, ThreadPool)은 이번 MVP 범위에서 제거하고 순차 격리 루프만 둔다(대규모 throughput은 향후 과제).

- [ ] **Step 4: Run tests to verify they pass**

Run: `python -m pytest tests/test_virtual_users_pipeline.py -q`
Expected: fault-isolation 테스트 PASS. 기존 pipeline 테스트 중 SourcePersona/concurrency 의존 테스트는 이 태스크에서 raw dict + `GenerationResult` 기준으로 수정한다 (아래 Step 5).

- [ ] **Step 5: Update remaining pipeline tests then commit**

기존 `tests/test_virtual_users_pipeline.py`의 각 테스트에서:
- `records`를 `build_fixture_raw_persona_records(...)`로 생성.
- 반환값을 `result = generate_virtual_user_batch(...)`로 받고 `batch = result.batch`로 접근.
- `RuleBasedVirtualUserGenerator()`를 generator로 사용.
- concurrency 테스트(`test_generate_virtual_user_batch_can_generate_users_concurrently`)는 제거한다(순차 루프로 단순화됨).

```bash
python -m pytest tests/test_virtual_users_pipeline.py -q
git add autoresearch/virtual_users/pipeline.py tests/test_virtual_users_pipeline.py
git commit -m "feat: isolate row failures and write quarantine in batch pipeline"
```

---

## Task 7: 옛 정규화 코드 제거 (contract 다이어트)

**Files:**
- Modify: `autoresearch/virtual_users/schema.py`, `persona_source.py`, `glm_generator.py`, `interests.py`
- Test: `tests/test_virtual_users_schema.py`, `test_virtual_users_persona_source.py`, `test_virtual_users_glm_generator.py`, `test_virtual_users_interests.py`

**Interfaces:**
- Removes: `SourcePersona`, `DerivedVirtualUserFeatures`, `source_persona_from_record`, `sample_personas_by_contract`, `build_fixture_persona_records`, `load_nvidia_persona_records`, `_virtual_user_from_derived_features`, `parse_virtual_user_json`, 옛 `build_virtual_user_prompt`(SourcePersona용).

- [ ] **Step 1: Find all references to removed symbols**

Run:
```bash
rg -n "SourcePersona|DerivedVirtualUserFeatures|source_persona_from_record|sample_personas_by_contract|build_fixture_persona_records|load_nvidia_persona_records|_virtual_user_from_derived_features|parse_virtual_user_json" autoresearch tests
```
Expected: 남은 참조 목록 확인 (production + 옛 테스트).

- [ ] **Step 2: Delete obsolete tests**

아래 옛 테스트를 제거한다 (새 태스크에서 대체됨):
- `test_virtual_users_schema.py`: `test_source_persona_normalizes_required_fields`, `test_source_persona_preserves_full_raw_persona_contract`, `test_derived_virtual_user_features_accepts_glm_only_payload`, `test_source_persona_accepts_spec_fields_and_kr_defaults`.
- `test_virtual_users_persona_source.py`: `test_source_persona_from_record_*`, `test_sample_personas_by_contract_*`(구 dict 아님 버전), `test_load_nvidia_persona_records_can_write_raw_snapshot`, `test_source_persona_from_record_maps_spec_fields`, `test_normalize_sex_*`는 유지.
- `test_virtual_users_glm_generator.py`: `test_build_virtual_user_prompt_contains_glm_json_contract`, `test_parse_virtual_user_json_*`, `test_virtual_user_from_derived_features_copies_source_fields_and_affinity`, `test_rule_based_generator_produces_valid_schema_without_api_call`.
- `test_virtual_users_interests.py`: `interests.py`를 삭제한다면 파일 전체 제거.

- [ ] **Step 3: Remove production symbols**

- `schema.py`: `SourcePersona`, `DerivedVirtualUserFeatures` 클래스 삭제.
- `persona_source.py`: `source_persona_from_record`, `sample_personas_by_contract`(구), `build_fixture_persona_records`(구), `load_nvidia_persona_records`, `_as_text`, `_as_text_list`, `_source_text`, `_source_hash` 삭제. `normalize_sex`, `record_age`, `record_sex`, `sample_raw_personas_by_contract`, `build_fixture_raw_persona_records`, `load_raw_persona_records`, `write_raw_persona_records` 유지. `SourcePersona` import 제거.
- `glm_generator.py`: 옛 `build_virtual_user_prompt`(SourcePersona용), `parse_virtual_user_json`, `_virtual_user_from_derived_features`, `interests`/`build_category_affinity` import 삭제. 새 함수만 남긴다.
- `interests.py`: rule-based generator가 더 이상 사용하지 않으므로 파일 삭제. `__init__` 등에서 참조 없으면 안전.

- [ ] **Step 4: Verify no dangling references**

Run:
```bash
rg -n "SourcePersona|DerivedVirtualUserFeatures|source_persona_from_record|parse_virtual_user_json|_virtual_user_from_derived_features|from autoresearch.virtual_users.interests" autoresearch tests
python -m pytest -q
python -m ruff check autoresearch tests
```
Expected: 참조 0건, 전체 테스트 PASS, ruff clean.

- [ ] **Step 5: Commit**

```bash
git add -A autoresearch/virtual_users tests
git commit -m "refactor: remove SourcePersona normalization layer and derived merge"
```

---

## Task 8: End-to-end 검증 (rule-based, API 없이)

**Files:**
- Test: `tests/test_virtual_users_pipeline.py`

**Interfaces:**
- Consumes: `build_fixture_raw_persona_records`, `generate_virtual_user_batch`, `RuleBasedVirtualUserGenerator`.

- [ ] **Step 1: Write end-to-end test**

```python
def test_end_to_end_100_rows_rule_based(tmp_path):
    records = build_fixture_raw_persona_records(male_count=60, female_count=60)
    request = GenerationRequest(
        male_count=50, female_count=50, use_llm=False,
        output_path=str(tmp_path / "vu.parquet"),
        warehouse_output_path=str(tmp_path / "vu.jsonl"),
        quarantine_output_path=str(tmp_path / "q.jsonl"),
    )

    result = generate_virtual_user_batch(request, records, RuleBasedVirtualUserGenerator())

    assert result.summary == {"valid": 100, "quarantined": 0,
                              "api_error": 0, "invalid_json": 0, "schema_fail": 0}
    warehouse_lines = (tmp_path / "vu.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(warehouse_lines) == 100
    assert result.batch.summary == {"total": 100, "male": 50, "female": 50}
```

- [ ] **Step 2: Run test**

Run: `python -m pytest tests/test_virtual_users_pipeline.py::test_end_to_end_100_rows_rule_based -q`
Expected: PASS.

- [ ] **Step 3: Full suite + lint**

Run:
```bash
python -m pytest -q
python -m ruff check autoresearch tests
```
Expected: all PASS, ruff clean.

- [ ] **Step 4: Commit**

```bash
git add tests/test_virtual_users_pipeline.py
git commit -m "test: end-to-end 100-row rule-based virtual user batch"
```

---

## Self-Review 결과

- **Spec coverage:** 정규화 제거(Task 1,2,7), LLM 전체 schema 생성 + 코드 stamp(Task 4), contract 다이어트(Task 7), 장애 격리 + quarantine(Task 3,6), raw dict 균형 샘플러(Task 1), 출력 동일 유지(Task 6,8), fallback 문서화(설계 문서 9절) — 모두 태스크로 커버됨.
- **범위 밖 확인:** 10만 건 실행 / resume / 대규모 throughput / action_log 시뮬레이터는 별도 spec (이 계획에서 병렬 생성은 의도적으로 제거).
- **Type consistency:** generator `.generate(raw_row, vid) -> str` + `.model_name`, `assemble_virtual_user(...) -> VirtualUser`, `generate_virtual_user_batch(...) -> GenerationResult`, `QuarantineRecord.error_type ∈ {api_error, invalid_json, schema_fail}` — 태스크 간 일치.
