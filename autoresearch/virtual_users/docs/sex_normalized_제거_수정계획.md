# sex_normalized 제거 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `SourcePersona.sex_normalized` 중복 필드를 제거하고, 원본 성별은 `raw_payload["sex"]`, 표준 성별은 `SourcePersona.sex`로 단순화한다.

**Architecture:** 현재 `source_persona_from_record()`는 `sex`와 `sex_normalized`에 같은 normalized value를 넣고 있어 정보가 중복된다. 원본 NVIDIA 값인 `남자`/`여자`는 이미 `raw_payload`에 보존되므로, runtime schema에서는 `sex`만 `male`/`female` 표준값으로 유지한다. 코드, 테스트, 계획 문서의 오래된 설명을 같은 방향으로 정리한다.

**Tech Stack:** Python, Pydantic v2, pytest, Markdown docs.

---

## 현재 상태와 결정

현재 raw persona는 성별을 아래처럼 제공한다.

```text
sex = "남자" | "여자"
```

현재 코드의 변환 결과는 아래와 같다.

```text
raw_payload["sex"] = "여자"
sex = "female"
sex_normalized = "female"
```

따라서 `sex_normalized`는 원본값 보존 역할을 하지 않는다. 원본값은 `raw_payload["sex"]`가 보존하고, pipeline 표준값은 `sex`가 담당한다.

최종 결정:

```text
raw_payload["sex"] = 원본 성별값, 예: "남자" | "여자"
SourcePersona.sex = 표준 성별값, "male" | "female"
SourcePersona.sex_normalized = 제거
```

이 결정은 Claude review 답변으로도 사용할 수 있다.

```text
sex_normalized는 초기 의도상 원본/정규화 값을 분리하려던 흔적으로 보이지만, 현재 구현에서는 sex와 동일한 normalized value만 담습니다. 원본 sex는 raw_payload에 보존되므로 schema 단순화를 위해 sex_normalized를 제거하고, SourcePersona.sex를 pipeline 표준값으로 유지하겠습니다.
```

## File Map

- Modify: `autoresearch/virtual_users/schema.py`
  - `SourcePersona.sex_normalized` 필드 제거
- Modify: `autoresearch/virtual_users/persona_source.py`
  - `SourcePersona(..., sex_normalized=normalized_sex)` 제거
- Modify: `tests/test_virtual_users_schema.py`
  - fixture에서 `sex_normalized` 제거
- Modify: `tests/test_virtual_users_persona_source.py`
  - `persona.sex_normalized` assertion 제거
  - `raw_payload["sex"]`와 `sex` 역할을 확인하는 assertion 추가
- Modify: `autoresearch/virtual_users/docs/무손실_persona_virtual_user_구현계획.md`
  - `sex_normalized` 설계 설명 제거 또는 `raw_payload["sex"]` 보존 설명으로 교체
- Modify: `autoresearch/virtual_users/docs/subagent_구현_실행계획.md`
  - `sex_normalized` 언급 제거

---

## Task 1: 테스트에서 새 성별 계약을 먼저 고정

**Files:**
- Modify: `tests/test_virtual_users_persona_source.py`
- Modify: `tests/test_virtual_users_schema.py`

- [ ] **Step 1: Update persona source test expectation**

`tests/test_virtual_users_persona_source.py`의 `test_source_persona_from_record_preserves_all_raw_columns`에서 아래 assertion을 제거한다.

```python
assert persona.sex_normalized == "female"
```

같은 위치에 아래 assertion을 추가한다.

```python
assert persona.sex == "female"
assert persona.raw_payload["sex"] == "여자"
```

최종 해당 부분은 아래처럼 된다.

```python
    assert persona.sex == "female"
    assert persona.raw_payload["sex"] == "여자"
    assert persona.country == "대한민국"
```

- [ ] **Step 2: Update schema fixture**

`tests/test_virtual_users_schema.py`의 `test_source_persona_preserves_full_raw_persona_contract`에서 `SourcePersona(...)` 호출의 아래 인자를 제거한다.

```python
sex_normalized="female",
```

그리고 원본 성별 보존을 명시하기 위해 `raw_payload`를 아래처럼 바꾼다.

```python
raw_payload={"uuid": "p-001", "sex": "여자"},
```

아래 assertion을 유지하거나 추가한다.

```python
assert persona.sex == "female"
assert persona.raw_payload["sex"] == "여자"
```

- [ ] **Step 3: Run tests to verify current code still has stale field**

```powershell
python -m pytest tests/test_virtual_users_schema.py::test_source_persona_preserves_full_raw_persona_contract tests/test_virtual_users_persona_source.py::test_source_persona_from_record_preserves_all_raw_columns -q
```

Expected:

```text
PASS before production field removal is also acceptable, because tests no longer require sex_normalized.
```

- [ ] **Step 4: Commit test contract change**

```powershell
git add tests/test_virtual_users_schema.py tests/test_virtual_users_persona_source.py
git commit -m "test: clarify source persona sex contract"
```

---

## Task 2: 코드에서 sex_normalized 제거

**Files:**
- Modify: `autoresearch/virtual_users/schema.py`
- Modify: `autoresearch/virtual_users/persona_source.py`

- [ ] **Step 1: Remove schema field**

`autoresearch/virtual_users/schema.py`의 `SourcePersona`에서 아래 줄을 제거한다.

```python
sex_normalized: Literal["male", "female"] | None = None
```

`SourcePersona`의 성별 필드는 아래처럼 남긴다.

```python
uuid: str
age: int
sex: Literal["male", "female"]
```

- [ ] **Step 2: Remove constructor argument**

`autoresearch/virtual_users/persona_source.py`의 `source_persona_from_record()`에서 아래 줄을 제거한다.

```python
sex_normalized=normalized_sex,
```

변환 책임은 아래처럼 유지한다.

```python
normalized_sex = normalize_sex(record["sex"])
raw_payload = dict(record)
persona = SourcePersona(
    uuid=_as_text(record, "uuid"),
    age=int(record["age"]),
    sex=normalized_sex,
    ...
    raw_payload=raw_payload,
)
```

- [ ] **Step 3: Verify no runtime references remain**

```powershell
rg -n "sex_normalized" autoresearch/virtual_users tests
```

Expected:

```text
No matches in runtime code or tests.
```

- [ ] **Step 4: Run focused tests**

```powershell
python -m pytest tests/test_virtual_users_schema.py tests/test_virtual_users_persona_source.py tests/test_virtual_users_glm_generator.py tests/test_virtual_users_pipeline.py -q
```

Expected:

```text
All focused tests pass.
```

- [ ] **Step 5: Commit runtime removal**

```powershell
git add autoresearch/virtual_users/schema.py autoresearch/virtual_users/persona_source.py
git commit -m "refactor: remove duplicate sex_normalized field"
```

---

## Task 3: 문서의 오래된 성별 설명 정리

**Files:**
- Modify: `autoresearch/virtual_users/docs/무손실_persona_virtual_user_구현계획.md`
- Modify: `autoresearch/virtual_users/docs/subagent_구현_실행계획.md`

- [ ] **Step 1: Update implementation plan helper fields**

`autoresearch/virtual_users/docs/무손실_persona_virtual_user_구현계획.md`에서 helper field 목록의 아래 줄을 제거한다.

```text
sex_normalized
```

대신 raw 보존 설명 근처에 아래 문장을 추가한다.

```text
원본 성별값은 `raw_payload["sex"]`에 보존하고, `SourcePersona.sex`는 pipeline 표준값인 `male` 또는 `female`로 유지한다.
```

- [ ] **Step 2: Update SourcePersona example in implementation plan**

같은 문서의 `SourcePersona` 예시에서 아래 줄을 제거한다.

```python
sex_normalized: Literal["male", "female"] | None = None
```

아래 형태로 정리한다.

```python
sex: Literal["male", "female"]
raw_payload: dict[str, object] = Field(default_factory=dict)
```

- [ ] **Step 3: Update subagent plan**

`autoresearch/virtual_users/docs/subagent_구현_실행계획.md`에서 아래 문장을 찾는다.

```text
`sex_normalized`, `country_code`, `locale`, `source_text`, `source_hash`, `raw_payload` 추가
```

아래처럼 바꾼다.

```text
`country_code`, `locale`, `source_text`, `source_hash`, `raw_payload` 추가
```

또 아래 문장을 찾는다.

```text
raw_payload/source_hash/source_text를 생성하고, 여자/남자 성별값을 정상화하라.
```

아래처럼 바꾼다.

```text
raw_payload/source_hash/source_text를 생성하고, 원본 여자/남자 성별값은 raw_payload에 보존하되 SourcePersona.sex는 male/female로 정상화하라.
```

- [ ] **Step 4: Verify docs and code references**

```powershell
rg -n "sex_normalized" autoresearch tests docs
```

Expected:

```text
No matches except this plan document if the plan remains as historical implementation guidance.
```

- [ ] **Step 5: Commit docs**

```powershell
git add autoresearch/virtual_users/docs/무손실_persona_virtual_user_구현계획.md autoresearch/virtual_users/docs/subagent_구현_실행계획.md
git commit -m "docs: clarify source persona sex normalization"
```

---

## Task 4: 최종 검증과 PR 리뷰 답변

**Files:**
- No production file changes expected.

- [ ] **Step 1: Run virtual user tests**

```powershell
python -m pytest tests/test_virtual_users_categories.py tests/test_virtual_users_schema.py tests/test_virtual_users_persona_source.py tests/test_virtual_users_interests.py tests/test_virtual_users_glm_generator.py tests/test_virtual_users_pipeline.py -q
```

Expected:

```text
All virtual user tests pass.
```

- [ ] **Step 2: Run whole suite**

```powershell
python -m pytest -q
```

Expected:

```text
All tests pass.
```

- [ ] **Step 3: Prepare Claude review reply**

PR comment에는 아래 내용으로 답한다.

```text
맞습니다. 초기 의도는 원본 sex와 normalized sex를 분리하려던 것이지만, 현재 구현에서는 raw_payload["sex"]가 원본 "남자"/"여자"를 보존하고 SourcePersona.sex가 이미 "male"/"female" 표준값입니다. 따라서 sex_normalized는 동일한 normalized value를 중복 저장하므로 제거했습니다. 이후 계약은 raw_payload["sex"] = 원본값, SourcePersona.sex = pipeline 표준값입니다.
```

- [ ] **Step 4: Push branch**

```powershell
git push
```

Expected:

```text
Existing PR #43 updates with the removal.
```

---

## Self-Review

- Spec coverage: Claude review의 중복 필드 지적을 코드, 테스트, 문서까지 반영하는 작업으로 나눴다.
- Original value preservation: 원본 성별은 `raw_payload["sex"]`, 표준 성별은 `SourcePersona.sex`라는 계약을 명확히 했다.
- Runtime simplicity: `sex_normalized` 제거 후 pipeline sampling, summary, GLM merge는 모두 `sex`만 사용한다.
- Type consistency: `SourcePersona.sex`는 계속 `Literal["male", "female"]`이며 raw source 값은 `raw_payload`에만 남긴다.
- Placeholder scan: 실행자가 채워야 하는 빈 단계 없이 명령과 기대 결과를 포함했다.
