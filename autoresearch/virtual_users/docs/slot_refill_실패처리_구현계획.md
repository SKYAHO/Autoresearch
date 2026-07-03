# Slot Refill 실패 처리 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** GLM 100건 생성 중 일부 persona가 실패해도 실패 row를 격리하고 다음 후보로 같은 성공 slot을 채워 `vu_0001`부터 `vu_0100`까지 gap 없는 결과를 만든다.

**Architecture:** 기존 기본 동작은 `fail_fast`로 유지한다. 새 `failure_policy="refill"`을 명시했을 때만 candidate pool을 넉넉히 섞고, 성공한 user에게만 slot index를 부여한다. 실패한 source persona는 `VirtualUserGenerationFailure`로 batch와 JSONL quarantine에 기록한다.

**Tech Stack:** Python, Pydantic v2, PyArrow Parquet, JSONL, pytest.

---

## 현재 문제

현재 `pipeline._generate_users()`는 `future.result()`에서 예외가 발생하면 batch 전체를 중단한다.

```text
한 건 실패
  -> _generate_users 예외
  -> VirtualUserBatch 생성 안 됨
  -> parquet 저장 안 됨
  -> warehouse JSONL 저장 안 됨
```

이 구조는 데이터 품질을 강하게 보려는 `fail_fast` 정책으로는 타당하다. 하지만 GLM 100건 생성에서는 한 persona 실패 때문에 이미 성공 가능한 나머지 후보를 버리는 비용이 크다.

단순히 실패 row를 skip하면 아래 문제가 생긴다.

```text
vu_0001
vu_0002
vu_0004
...
```

또는 재번호를 매기면 원래 source persona와 virtual user id 매핑이 흔들린다.

따라서 `refill` 정책에서는 source candidate index가 아니라 성공 slot index를 기준으로 `virtual_user_id`를 부여한다.

```text
slot 1 -> candidate A 실패 -> failure log
slot 1 -> candidate B 성공 -> vu_0001
slot 2 -> candidate C 성공 -> vu_0002
...
slot 100 -> candidate Z 성공 -> vu_0100
```

## 구현 범위

이번 구현은 안전한 v1 범위로 제한한다.

- `failure_policy="fail_fast"`: 현재 동작 유지
- `failure_policy="refill"`: sequential refill만 지원
- `failure_policy="refill"`과 `max_concurrency > 1` 조합은 명시적으로 거부
- 최종 성공 row는 요청한 `male_count`, `female_count`를 만족해야 함
- 실패 row는 parquet/warehouse output에 섞지 않음
- 실패 row는 `VirtualUserBatch.failures`와 failure JSONL에 기록
- 성공한 user의 `virtual_user_id`는 gap 없이 `vu_0001`부터 부여

## File Map

- Modify: `autoresearch/virtual_users/schema.py`
  - `GenerationRequest.failure_policy`
  - `GenerationRequest.max_refill_attempts`
  - `GenerationRequest.failure_output_path`
  - `VirtualUserGenerationFailure`
  - `VirtualUserBatch.failures`
  - `VirtualUserBatch.summary["failed"]`
- Modify: `autoresearch/virtual_users/persona_source.py`
  - `sample_persona_candidates_by_contract()` 추가
- Modify: `autoresearch/virtual_users/pipeline.py`
  - refill 생성 경로 추가
  - failure JSONL writer 추가
  - `fail_fast` 기존 경로 유지
- Modify: `tests/test_virtual_users_schema.py`
  - request/failure schema 테스트 추가
- Modify: `tests/test_virtual_users_persona_source.py`
  - candidate pool deterministic 테스트 추가
- Modify: `tests/test_virtual_users_pipeline.py`
  - fail-fast 명시 테스트 추가
  - refill 성공 테스트 추가
  - refill 후보 소진 테스트 추가
- Modify: `autoresearch/virtual_users/docs/lossless_persona_virtual_user_qa.md`
  - refill policy 실행/검증 항목 추가

---

## Task 1: Schema에 실패 정책과 failure record 추가

**Files:**
- Modify: `autoresearch/virtual_users/schema.py`
- Test: `tests/test_virtual_users_schema.py`

- [ ] **Step 1: Write the failing schema tests**

`tests/test_virtual_users_schema.py` import에 `VirtualUserGenerationFailure`를 추가한다.

```python
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    DerivedVirtualUserFeatures,
    GenerationRequest,
    SourcePersona,
    VirtualUser,
    VirtualUserBatch,
    VirtualUserGenerationFailure,
    YouTubeProfile,
    age_bucket_for_age,
)
```

아래 테스트를 추가한다.

```python
def test_generation_request_accepts_refill_failure_policy():
    request = GenerationRequest(
        failure_policy="refill",
        max_refill_attempts=150,
        failure_output_path="data/generated/virtual_user_failures.jsonl",
    )

    assert request.failure_policy == "refill"
    assert request.max_refill_attempts == 150
    assert request.failure_output_path.endswith("virtual_user_failures.jsonl")


def test_generation_request_rejects_non_positive_refill_attempts():
    with pytest.raises(ValueError, match="max_refill_attempts must be at least 1"):
        GenerationRequest(max_refill_attempts=0)


def test_virtual_user_batch_summary_counts_failures():
    failure = VirtualUserGenerationFailure(
        slot_index=1,
        attempt_index=1,
        source_uuid="source-001",
        source_hash="hash-001",
        sex="male",
        age=24,
        error_type="ValueError",
        error_message="Unsupported categories: ['Travel']",
    )
    batch = VirtualUserBatch(
        schema_version=GENERATION_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        source_dataset="nvidia/Nemotron-Personas-Korea",
        request=GenerationRequest(male_count=1, female_count=0),
        users=[],
        failures=[failure],
    )

    assert batch.summary["total"] == 0
    assert batch.summary["male"] == 0
    assert batch.summary["female"] == 0
    assert batch.summary["failed"] == 1
    assert batch.to_output_dict()["failures"][0]["source_uuid"] == "source-001"
```

- [ ] **Step 2: Run tests to verify they fail**

```powershell
python -m pytest tests/test_virtual_users_schema.py::test_generation_request_accepts_refill_failure_policy tests/test_virtual_users_schema.py::test_generation_request_rejects_non_positive_refill_attempts tests/test_virtual_users_schema.py::test_virtual_user_batch_summary_counts_failures -q
```

Expected:

```text
ImportError 또는 AttributeError: VirtualUserGenerationFailure / failure_policy / max_refill_attempts 없음
```

- [ ] **Step 3: Add schema fields and failure model**

`autoresearch/virtual_users/schema.py`에서 `GenerationRequest`에 필드를 추가한다.

```python
class GenerationRequest(BaseModel):
    """가상 사용자 배치 생성에 필요한 입력 조건과 출력 경로를 담는다."""

    age_min: int = 20
    age_max: int = 29
    male_count: int = 50
    female_count: int = 50
    seed: int = 42
    use_llm: bool = True
    max_concurrency: int = 1
    failure_policy: Literal["fail_fast", "refill"] = "fail_fast"
    max_refill_attempts: int = 300
    source_mode: Literal["huggingface", "fixture"] = "huggingface"
    output_path: str = "asset/virtual_user/virtual_users_20s_100.parquet"
    raw_output_path: str = "data/raw/personas/nvidia_personas_kr.jsonl"
    warehouse_output_path: str = "data/generated/virtual_users_kr.jsonl"
    failure_output_path: str = "data/generated/virtual_user_failures.jsonl"
```

`GenerationRequest`에 validator를 추가한다.

```python
    @field_validator("max_refill_attempts")
    @classmethod
    def positive_max_refill_attempts(cls, value: int) -> int:
        """refill 정책에서 시도할 최대 candidate 수가 1 이상인지 확인한다."""

        if value < 1:
            raise ValueError("max_refill_attempts must be at least 1")
        return value
```

`GenerationMeta` 아래에 failure model을 추가한다.

```python
class VirtualUserGenerationFailure(BaseModel):
    """refill 정책에서 버려진 source persona와 실패 원인을 기록한다."""

    slot_index: int
    attempt_index: int
    source_uuid: str
    source_hash: str = ""
    sex: Literal["male", "female"]
    age: int
    error_type: str
    error_message: str
```

`VirtualUserBatch`에 `failures` 필드를 추가한다.

```python
class VirtualUserBatch(BaseModel):
    """여러 명의 virtual user와 생성 요청 정보를 함께 보관하는 batch 결과."""

    schema_version: str
    prompt_version: str
    source_dataset: str
    request: GenerationRequest
    users: list[VirtualUser]
    failures: list[VirtualUserGenerationFailure] = Field(default_factory=list)
    generated_at: str = Field(
        default_factory=lambda: datetime.now(UTC).replace(microsecond=0).isoformat()
    )
```

`summary` 반환값에 `failed`를 추가한다.

```python
    @property
    def summary(self) -> dict[str, int]:
        """생성된 batch의 총원, 성별 분포, 실패 건수를 계산한다."""

        male = sum(1 for user in self.users if user.sex == "male")
        female = sum(1 for user in self.users if user.sex == "female")
        return {
            "total": len(self.users),
            "male": male,
            "female": female,
            "failed": len(self.failures),
        }
```

- [ ] **Step 4: Run schema tests**

```powershell
python -m pytest tests/test_virtual_users_schema.py -q
```

Expected:

```text
All schema tests pass.
```

- [ ] **Step 5: Commit schema contract**

```powershell
git add autoresearch/virtual_users/schema.py tests/test_virtual_users_schema.py
git commit -m "feat: add virtual user refill failure schema"
```

---

## Task 2: Candidate pool sampling 추가

**Files:**
- Modify: `autoresearch/virtual_users/persona_source.py`
- Test: `tests/test_virtual_users_persona_source.py`

- [ ] **Step 1: Write failing candidate pool tests**

`tests/test_virtual_users_persona_source.py` import에 `sample_persona_candidates_by_contract`를 추가한다.

```python
from autoresearch.virtual_users.persona_source import (
    build_fixture_persona_records,
    normalize_sex,
    sample_persona_candidates_by_contract,
    sample_personas_by_contract,
    source_persona_from_record,
)
```

아래 테스트를 추가한다.

```python
def test_sample_persona_candidates_by_contract_returns_deterministic_candidate_pool():
    records = build_fixture_persona_records(male_count=4, female_count=4)

    first = sample_persona_candidates_by_contract(
        records=records,
        age_min=20,
        age_max=29,
        male_count=2,
        female_count=2,
        seed=123,
    )
    second = sample_persona_candidates_by_contract(
        records=records,
        age_min=20,
        age_max=29,
        male_count=2,
        female_count=2,
        seed=123,
    )

    assert [record.uuid for record in first] == [record.uuid for record in second]
    assert len(first) == 8
    assert sum(1 for record in first if record.sex == "male") == 4
    assert sum(1 for record in first if record.sex == "female") == 4


def test_sample_persona_candidates_by_contract_requires_requested_minimum_counts():
    records = build_fixture_persona_records(male_count=1, female_count=1)

    with pytest.raises(ValueError, match="Not enough male personas"):
        sample_persona_candidates_by_contract(
            records=records,
            age_min=20,
            age_max=29,
            male_count=2,
            female_count=1,
            seed=1,
        )
```

- [ ] **Step 2: Run tests to verify they fail**

```powershell
python -m pytest tests/test_virtual_users_persona_source.py::test_sample_persona_candidates_by_contract_returns_deterministic_candidate_pool tests/test_virtual_users_persona_source.py::test_sample_persona_candidates_by_contract_requires_requested_minimum_counts -q
```

Expected:

```text
ImportError: cannot import name 'sample_persona_candidates_by_contract'
```

- [ ] **Step 3: Add candidate pool function**

`autoresearch/virtual_users/persona_source.py`의 `sample_personas_by_contract()` 아래에 추가한다.

```python
def sample_persona_candidates_by_contract(
    records: Iterable[SourcePersona],
    age_min: int,
    age_max: int,
    male_count: int,
    female_count: int,
    seed: int,
) -> list[SourcePersona]:
    """refill 생성을 위해 조건에 맞는 후보 전체를 seed 기반으로 섞어 반환한다."""

    eligible = [record for record in records if age_min <= record.age <= age_max]
    male_records = [record for record in eligible if record.sex == "male"]
    female_records = [record for record in eligible if record.sex == "female"]

    logger.info(
        "Prepared source persona candidates for refill generation",
        extra={
            "age_min": age_min,
            "age_max": age_max,
            "eligible_count": len(eligible),
            "available_male_count": len(male_records),
            "available_female_count": len(female_records),
            "requested_male_count": male_count,
            "requested_female_count": female_count,
            "seed": seed,
        },
    )

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
    candidate_pool = list(male_records) + list(female_records)
    rng.shuffle(candidate_pool)
    return candidate_pool
```

- [ ] **Step 4: Run persona source tests**

```powershell
python -m pytest tests/test_virtual_users_persona_source.py -q
```

Expected:

```text
All persona source tests pass.
```

- [ ] **Step 5: Commit candidate pool helper**

```powershell
git add autoresearch/virtual_users/persona_source.py tests/test_virtual_users_persona_source.py
git commit -m "feat: add refill persona candidate sampling"
```

---

## Task 3: 현재 fail-fast 정책을 명시 테스트로 고정

**Files:**
- Modify: `tests/test_virtual_users_pipeline.py`

- [ ] **Step 1: Add pytest import and failing generator**

`tests/test_virtual_users_pipeline.py` 상단 import에 `pytest`를 추가한다.

```python
import pytest
```

테스트 helper class를 추가한다.

```python
class AlwaysFailGenerator:
    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        raise ValueError(f"bad source persona: {persona.uuid}")
```

- [ ] **Step 2: Write fail-fast behavior test**

아래 테스트를 추가한다.

```python
def test_generate_virtual_user_batch_fail_fast_raises_and_writes_no_outputs(tmp_path):
    records = build_fixture_persona_records(male_count=2, female_count=0)
    output_path = tmp_path / "users.parquet"
    warehouse_output_path = tmp_path / "users.jsonl"
    request = GenerationRequest(
        male_count=1,
        female_count=0,
        seed=17,
        use_llm=False,
        source_mode="fixture",
        failure_policy="fail_fast",
        output_path=str(output_path),
        warehouse_output_path=str(warehouse_output_path),
    )

    with pytest.raises(ValueError, match="bad source persona"):
        generate_virtual_user_batch(
            request=request,
            records=records,
            generator=AlwaysFailGenerator(),
        )

    assert not output_path.exists()
    assert not warehouse_output_path.exists()
```

- [ ] **Step 3: Run test**

```powershell
python -m pytest tests/test_virtual_users_pipeline.py::test_generate_virtual_user_batch_fail_fast_raises_and_writes_no_outputs -q
```

Expected:

```text
PASS
```

이 테스트는 현재 동작을 고정한다. 실패하면 최근 코드가 이미 fail-fast 정책을 바꾼 것이므로 `pipeline.py`를 확인한다.

- [ ] **Step 4: Commit fail-fast regression test**

```powershell
git add tests/test_virtual_users_pipeline.py
git commit -m "test: document virtual user fail-fast generation"
```

---

## Task 4: Refill 생성 경로 구현

**Files:**
- Modify: `autoresearch/virtual_users/pipeline.py`
- Modify: `tests/test_virtual_users_pipeline.py`

- [ ] **Step 1: Write refill success test**

`tests/test_virtual_users_pipeline.py`에 helper generator를 추가한다.

```python
class FirstCallFailsGenerator:
    def __init__(self) -> None:
        self.calls = 0
        self.delegate = RuleBasedVirtualUserGenerator(model_name="refill-fixture")

    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        self.calls += 1
        if self.calls == 1:
            raise ValueError("GLM returned invalid derived JSON")
        return self.delegate.generate(persona, virtual_user_id)
```

아래 테스트를 추가한다.

```python
def test_generate_virtual_user_batch_refills_failed_persona_without_id_gap(tmp_path):
    records = build_fixture_persona_records(male_count=4, female_count=0)
    output_path = tmp_path / "users.parquet"
    warehouse_output_path = tmp_path / "users.jsonl"
    failure_output_path = tmp_path / "failures.jsonl"
    request = GenerationRequest(
        male_count=2,
        female_count=0,
        seed=17,
        use_llm=False,
        source_mode="fixture",
        failure_policy="refill",
        max_refill_attempts=4,
        output_path=str(output_path),
        warehouse_output_path=str(warehouse_output_path),
        failure_output_path=str(failure_output_path),
    )

    batch = generate_virtual_user_batch(
        request=request,
        records=records,
        generator=FirstCallFailsGenerator(),
    )

    assert [user.virtual_user_id for user in batch.users] == ["vu_0001", "vu_0002"]
    assert batch.summary["total"] == 2
    assert batch.summary["male"] == 2
    assert batch.summary["female"] == 0
    assert batch.summary["failed"] == 1
    assert output_path.exists()
    assert warehouse_output_path.exists()
    failure_rows = [
        json.loads(line)
        for line in failure_output_path.read_text(encoding="utf-8").splitlines()
    ]
    assert len(failure_rows) == 1
    assert failure_rows[0]["slot_index"] == 1
    assert failure_rows[0]["attempt_index"] == 1
    assert failure_rows[0]["error_type"] == "ValueError"
    assert "invalid derived JSON" in failure_rows[0]["error_message"]
```

- [ ] **Step 2: Write refill exhaustion test**

아래 테스트를 추가한다.

```python
def test_generate_virtual_user_batch_refill_raises_when_candidates_are_exhausted(
    tmp_path,
):
    records = build_fixture_persona_records(male_count=2, female_count=0)
    output_path = tmp_path / "users.parquet"
    warehouse_output_path = tmp_path / "users.jsonl"
    failure_output_path = tmp_path / "failures.jsonl"
    request = GenerationRequest(
        male_count=1,
        female_count=0,
        seed=17,
        use_llm=False,
        source_mode="fixture",
        failure_policy="refill",
        max_refill_attempts=2,
        output_path=str(output_path),
        warehouse_output_path=str(warehouse_output_path),
        failure_output_path=str(failure_output_path),
    )

    with pytest.raises(RuntimeError, match="Unable to fill virtual user batch"):
        generate_virtual_user_batch(
            request=request,
            records=records,
            generator=AlwaysFailGenerator(),
        )

    assert failure_output_path.exists()
    assert len(failure_output_path.read_text(encoding="utf-8").splitlines()) == 2
    assert not output_path.exists()
    assert not warehouse_output_path.exists()
```

- [ ] **Step 3: Write refill concurrency guard test**

아래 테스트를 추가한다.

```python
def test_generate_virtual_user_batch_refill_rejects_concurrency_for_stable_slots(
    tmp_path,
):
    records = build_fixture_persona_records(male_count=2, female_count=0)
    request = GenerationRequest(
        male_count=1,
        female_count=0,
        seed=17,
        use_llm=False,
        source_mode="fixture",
        failure_policy="refill",
        max_concurrency=2,
        output_path=str(tmp_path / "users.parquet"),
    )

    with pytest.raises(ValueError, match="refill failure_policy requires max_concurrency=1"):
        generate_virtual_user_batch(
            request=request,
            records=records,
            generator=RuleBasedVirtualUserGenerator(),
        )
```

- [ ] **Step 4: Run tests to verify they fail**

```powershell
python -m pytest tests/test_virtual_users_pipeline.py::test_generate_virtual_user_batch_refills_failed_persona_without_id_gap tests/test_virtual_users_pipeline.py::test_generate_virtual_user_batch_refill_raises_when_candidates_are_exhausted tests/test_virtual_users_pipeline.py::test_generate_virtual_user_batch_refill_rejects_concurrency_for_stable_slots -q
```

Expected:

```text
FAIL because pipeline does not implement refill policy yet.
```

- [ ] **Step 5: Add pipeline imports**

`autoresearch/virtual_users/pipeline.py` import를 수정한다.

```python
from autoresearch.virtual_users.persona_source import (
    sample_persona_candidates_by_contract,
    sample_personas_by_contract,
)
from autoresearch.virtual_users.schema import (
    GENERATION_SCHEMA_VERSION,
    PROMPT_VERSION,
    SOURCE_DATASET,
    GenerationRequest,
    SourcePersona,
    VirtualUser,
    VirtualUserBatch,
    VirtualUserGenerationFailure,
)
```

- [ ] **Step 6: Add failure writer and failure record helper**

`write_virtual_users_warehouse_jsonl()` 아래에 추가한다.

```python
def write_virtual_user_failures_jsonl(
    failures: list[VirtualUserGenerationFailure],
    output_path: str | Path,
) -> None:
    """refill 정책에서 버린 source persona 실패 기록을 JSONL로 저장한다."""

    if not failures:
        return

    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8") as file:
        for failure in failures:
            file.write(json.dumps(failure.model_dump(), ensure_ascii=False) + "\n")
    logger.info(
        "Wrote virtual user generation failures",
        extra={"output_path": str(path), "failed_count": len(failures)},
    )


def _generation_failure(
    persona: SourcePersona,
    slot_index: int,
    attempt_index: int,
    exc: Exception,
) -> VirtualUserGenerationFailure:
    """실패한 candidate를 quarantine row로 변환한다."""

    return VirtualUserGenerationFailure(
        slot_index=slot_index,
        attempt_index=attempt_index,
        source_uuid=persona.uuid,
        source_hash=persona.source_hash,
        sex=persona.sex,
        age=persona.age,
        error_type=type(exc).__name__,
        error_message=str(exc),
    )
```

- [ ] **Step 7: Add refill generation helper**

`_generate_users()` 아래에 추가한다.

```python
def _generate_users_with_refill(
    candidates: list[SourcePersona],
    generator: VirtualUserGenerator,
    male_count: int,
    female_count: int,
    max_refill_attempts: int,
) -> tuple[list[VirtualUser], list[VirtualUserGenerationFailure]]:
    """실패 candidate를 버리고 다음 candidate로 gap 없는 user slot을 채운다."""

    users: list[VirtualUser] = []
    failures: list[VirtualUserGenerationFailure] = []
    accepted_by_sex = {"male": 0, "female": 0}
    requested_by_sex = {"male": male_count, "female": female_count}
    target_total = male_count + female_count
    attempt_index = 0

    for persona in candidates:
        if len(users) >= target_total:
            break
        if accepted_by_sex[persona.sex] >= requested_by_sex[persona.sex]:
            continue
        if attempt_index >= max_refill_attempts:
            break

        slot_index = len(users) + 1
        attempt_index += 1
        try:
            _, user = _generate_one_user(generator, slot_index, persona)
        except Exception as exc:
            failures.append(
                _generation_failure(
                    persona=persona,
                    slot_index=slot_index,
                    attempt_index=attempt_index,
                    exc=exc,
                )
            )
            logger.warning(
                "Discarded failed virtual user candidate",
                extra={
                    "source_uuid": persona.uuid,
                    "slot_index": slot_index,
                    "attempt_index": attempt_index,
                    "error_type": type(exc).__name__,
                },
            )
            continue

        users.append(user)
        accepted_by_sex[user.sex] += 1

    return users, failures
```

- [ ] **Step 8: Route generate_virtual_user_batch by failure policy**

`generate_virtual_user_batch()`에서 기존 sampled/users 생성 구간을 아래 형태로 바꾼다.

```python
    failures: list[VirtualUserGenerationFailure] = []
    if request.failure_policy == "refill":
        if request.max_concurrency != 1:
            raise ValueError("refill failure_policy requires max_concurrency=1")

        candidates = sample_persona_candidates_by_contract(
            records=records,
            age_min=request.age_min,
            age_max=request.age_max,
            male_count=request.male_count,
            female_count=request.female_count,
            seed=request.seed,
        )
        logger.info(
            "Prepared personas for refill batch generation",
            extra={"candidate_count": len(candidates)},
        )
        users, failures = _generate_users_with_refill(
            candidates=candidates,
            generator=generator,
            male_count=request.male_count,
            female_count=request.female_count,
            max_refill_attempts=request.max_refill_attempts,
        )
        write_virtual_user_failures_jsonl(
            failures=failures,
            output_path=request.failure_output_path,
        )
        if len(users) != request.male_count + request.female_count:
            raise RuntimeError(
                "Unable to fill virtual user batch: "
                f"requested={request.male_count + request.female_count}, "
                f"generated={len(users)}, failed={len(failures)}"
            )
    else:
        sampled = sample_personas_by_contract(
            records=records,
            age_min=request.age_min,
            age_max=request.age_max,
            male_count=request.male_count,
            female_count=request.female_count,
            seed=request.seed,
        )
        logger.info(
            "Sampled personas for batch generation",
            extra={"sampled_count": len(sampled)},
        )
        users = _generate_users(
            sampled=sampled,
            generator=generator,
            max_concurrency=request.max_concurrency,
        )
```

`VirtualUserBatch` 생성부에 `failures=failures`를 추가한다.

```python
    batch = VirtualUserBatch(
        schema_version=GENERATION_SCHEMA_VERSION,
        prompt_version=PROMPT_VERSION,
        source_dataset=SOURCE_DATASET,
        request=request,
        users=users,
        failures=failures,
    )
```

- [ ] **Step 9: Run pipeline refill tests**

```powershell
python -m pytest tests/test_virtual_users_pipeline.py::test_generate_virtual_user_batch_refills_failed_persona_without_id_gap tests/test_virtual_users_pipeline.py::test_generate_virtual_user_batch_refill_raises_when_candidates_are_exhausted tests/test_virtual_users_pipeline.py::test_generate_virtual_user_batch_refill_rejects_concurrency_for_stable_slots -q
```

Expected:

```text
3 passed
```

- [ ] **Step 10: Run all pipeline tests**

```powershell
python -m pytest tests/test_virtual_users_pipeline.py -q
```

Expected:

```text
All pipeline tests pass.
```

- [ ] **Step 11: Commit refill pipeline**

```powershell
git add autoresearch/virtual_users/pipeline.py tests/test_virtual_users_pipeline.py
git commit -m "feat: refill failed virtual user generation slots"
```

---

## Task 5: QA 문서에 refill 운영 정책 추가

**Files:**
- Modify: `autoresearch/virtual_users/docs/lossless_persona_virtual_user_qa.md`

- [ ] **Step 1: Add refill policy section**

`autoresearch/virtual_users/docs/lossless_persona_virtual_user_qa.md`의 100건 생성 섹션 뒤에 아래 내용을 추가한다.

```markdown
## 3-1. Refill failure policy

운영성 100건 생성에서는 GLM 응답 1건 실패 때문에 전체 batch를 버리지 않기 위해 `failure_policy="refill"`을 사용할 수 있다.

정책:

```text
성공한 row에만 virtual_user_id를 부여한다.
실패한 source persona는 output parquet/warehouse JSONL에 넣지 않는다.
실패한 source persona는 failure JSONL에 기록한다.
최종 output은 요청한 male_count/female_count를 만족해야 한다.
refill 정책은 max_concurrency=1에서만 사용한다.
```

권장 request 값:

```python
request = GenerationRequest(
    male_count=50,
    female_count=50,
    seed=42,
    use_llm=True,
    max_concurrency=1,
    failure_policy="refill",
    max_refill_attempts=300,
    source_mode="huggingface",
    output_path="asset/virtual_user/virtual_users_20s_100.parquet",
    warehouse_output_path="data/generated/virtual_users_kr.jsonl",
    failure_output_path="data/generated/virtual_user_failures.jsonl",
)
```

검증:

```powershell
@'
from pathlib import Path
import json

warehouse_path = Path("data/generated/virtual_users_kr.jsonl")
failure_path = Path("data/generated/virtual_user_failures.jsonl")

warehouse_rows = warehouse_path.read_text(encoding="utf-8").splitlines()
failure_rows = (
    failure_path.read_text(encoding="utf-8").splitlines()
    if failure_path.exists()
    else []
)

print(f"warehouse_jsonl_lines={len(warehouse_rows)}")
print(f"failure_jsonl_lines={len(failure_rows)}")

if failure_rows:
    first_failure = json.loads(failure_rows[0])
    print(f"first_failure_type={first_failure['error_type']}")
'@ | python -
```
```

- [ ] **Step 2: Run markdown sanity check**

```powershell
Get-Content -Encoding UTF8 autoresearch/virtual_users/docs/lossless_persona_virtual_user_qa.md | Select-String -Pattern "Refill failure policy"
```

Expected:

```text
Line containing "Refill failure policy"
```

- [ ] **Step 3: Commit QA docs**

```powershell
git add autoresearch/virtual_users/docs/lossless_persona_virtual_user_qa.md
git commit -m "docs: document virtual user refill failure policy"
```

---

## Task 6: 최종 검증

**Files:**
- No production file changes expected.

- [ ] **Step 1: Run focused tests**

```powershell
python -m pytest tests/test_virtual_users_schema.py tests/test_virtual_users_persona_source.py tests/test_virtual_users_pipeline.py -q
```

Expected:

```text
All focused tests pass.
```

- [ ] **Step 2: Run virtual user test suite**

```powershell
python -m pytest tests/test_virtual_users_categories.py tests/test_virtual_users_schema.py tests/test_virtual_users_persona_source.py tests/test_virtual_users_interests.py tests/test_virtual_users_glm_generator.py tests/test_virtual_users_pipeline.py -q
```

Expected:

```text
All virtual user tests pass.
```

- [ ] **Step 3: Run whole suite**

```powershell
python -m pytest -q
```

Expected:

```text
All tests pass.
```

- [ ] **Step 4: Verify no generated data is staged**

```powershell
git status --short --untracked-files=all
```

Expected:

```text
Code, test, and docs changes only. No Nemotron-Personas-Korea, asset, or data files staged.
```

- [ ] **Step 5: Push branch**

```powershell
git push
```

Expected:

```text
Branch updates the existing PR #43.
```

---

## Owner Notes

이 설계는 두 정책을 명확히 분리한다.

```text
fail_fast:
  품질 계약 위반을 즉시 드러낸다.
  partial output을 만들지 않는다.
  concurrency를 계속 허용한다.

refill:
  운영용 100건 산출을 우선한다.
  실패 persona는 quarantine으로 분리한다.
  성공 slot 기준으로 virtual_user_id를 부여한다.
  max_concurrency=1로 deterministic slot refill을 보장한다.
```

Claude review에 대한 설명은 아래처럼 정리할 수 있다.

```text
기존 경로는 fail_fast 정책이므로 한 건 실패가 batch 전체 실패로 전파된다.
새 refill 정책은 이 문제를 해결하기 위해 별도 opt-in으로 추가한다.
refill에서는 실패 candidate를 output에 섞지 않고 failure JSONL로 기록한다.
virtual_user_id는 source candidate index가 아니라 성공 slot index에서 생성하므로 gap이 없다.
```

## Self-Review

- Spec coverage: 중간 실패를 trash/quarantine하고 다음 candidate로 이어서 100건을 채우는 요구를 Task 1, Task 2, Task 4에서 구현한다.
- Index stability: `virtual_user_id`를 `len(users) + 1` 성공 slot 기준으로 부여해 실패 candidate가 id gap을 만들지 않도록 했다.
- Existing behavior: `failure_policy="fail_fast"` 기본값과 Task 3 테스트로 현재 동작을 유지한다.
- Concurrency: refill v1은 `max_concurrency=1`만 허용해 동시 완료 순서에 따른 nondeterministic refill을 차단한다.
- Placeholder scan: 계획 안에 비어 있는 작업이나 미정 항목이 없다.
- Type consistency: `GenerationRequest`, `VirtualUserGenerationFailure`, `VirtualUserBatch.failures`, `write_virtual_user_failures_jsonl` 이름을 모든 task에서 동일하게 사용했다.
