# GLM-Only Cleanup Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** legacy provider 삭제 이후 남은 GLM-only virtual user 코드에서 중복 abstraction과 dead constants를 제거한다.

**Architecture:** legacy provider는 이미 삭제되었으므로 provider 공통화 작업은 종료한다. 남은 작업은 GLM 단일 경로를 짧게 만드는 삭제 리팩토링이다: category vocabulary는 `categories.py` 하나만 source of truth로 두고, pipeline Protocol 하나만 유지하며, post-merge self-check와 작은 중복 검사를 제거한다.

**Tech Stack:** Python, Pydantic v2, PyArrow, pytest.

---

## Current State

완료된 항목:

- Multi-provider prompt/parser/merge 공통화: legacy provider 삭제로 해결됨. 더 이상 공통화 대상 없음.
- legacy rule-based fallback 제거: provider 파일과 해당 테스트 삭제됨.

남은 항목:

- category vocab 하나로 통합
- provider별 Protocol 제거
- post-merge self-check 제거
- 작은 dead constant/중복 검사 정리

## File Map

- Modify: `autoresearch/virtual_users/schema.py`
  - `YOUTUBE_CATEGORIES`, `WATCH_TIME_BANDS` 제거
- Modify: `autoresearch/virtual_users/interests.py`
  - category vocab import를 `categories.DEFAULT_KAGGLE_YOUTUBE_CATEGORIES`로 변경
- Modify: `autoresearch/virtual_users/glm_generator.py`
  - file-local `VirtualUserGenerator` Protocol 제거
  - `_ensure_source_persona_matches_user()` 제거
  - `_virtual_user_from_derived_features()`의 self-check 호출 제거
- Modify: `autoresearch/virtual_users/categories.py`
  - duplicate category 검사 간소화
- Modify: `tests/test_virtual_users_glm_generator.py`
  - self-check 전용 테스트 제거
  - merge 테스트로 factual copy 보장 유지
- Modify: `tests/test_virtual_users_interests.py`
  - Kaggle category vocab 전환 후 deterministic fallback 기대값 확인
- Modify: `tests/test_virtual_users_categories.py`
  - duplicate category 테스트 유지, 에러 메시지 기대값만 단순화 가능

---

## Task 1: Category Vocab One Source

**Files:**
- Modify: `autoresearch/virtual_users/schema.py`
- Modify: `autoresearch/virtual_users/interests.py`
- Test: `tests/test_virtual_users_interests.py`

- [ ] **Step 1: Write the failing test**

Add this test to `tests/test_virtual_users_interests.py`:

```python
from autoresearch.virtual_users.categories import DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
from autoresearch.virtual_users.interests import CATEGORY_KEYWORDS


def test_interest_category_keywords_use_kaggle_vocab():
    assert set(CATEGORY_KEYWORDS).issubset(set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES))
    assert "Travel & Events" in DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
```

- [ ] **Step 2: Run test to verify it fails or exposes old coupling**

Run:

```powershell
python -m pytest tests/test_virtual_users_interests.py::test_interest_category_keywords_use_kaggle_vocab -q
```

Expected before implementation: pass may already happen for existing keys, but `interests.py` still imports `YOUTUBE_CATEGORIES` from `schema.py`; continue to Step 3 because the code dependency is the target.

- [ ] **Step 3: Replace schema vocab import**

In `autoresearch/virtual_users/interests.py`, replace:

```python
from autoresearch.virtual_users.schema import SourcePersona, YOUTUBE_CATEGORIES
```

with:

```python
from autoresearch.virtual_users.categories import DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
from autoresearch.virtual_users.schema import SourcePersona
```

Then replace:

```python
for category in YOUTUBE_CATEGORIES:
```

with:

```python
for category in DEFAULT_KAGGLE_YOUTUBE_CATEGORIES:
```

- [ ] **Step 4: Delete dead constants**

In `autoresearch/virtual_users/schema.py`, delete:

```python
YOUTUBE_CATEGORIES = [
    "Gaming",
    "Music",
    "Entertainment",
    "Education",
    "News & Politics",
    "Sports",
    "Science & Technology",
    "Howto & Style",
    "People & Blogs",
    "Comedy",
]

WATCH_TIME_BANDS = ["morning", "afternoon", "evening", "night", "mixed"]
```

- [ ] **Step 5: Verify no old constants remain**

Run:

```powershell
rg -n "YOUTUBE_CATEGORIES|WATCH_TIME_BANDS" autoresearch tests
python -m pytest tests/test_virtual_users_interests.py tests/test_virtual_users_categories.py -q
```

Expected:

```text
rg exits with no matches
tests pass
```

---

## Task 2: Remove Provider-Local Protocol

**Files:**
- Modify: `autoresearch/virtual_users/glm_generator.py`
- Test: `tests/test_virtual_users_glm_generator.py`
- Test: `tests/test_virtual_users_pipeline.py`

- [ ] **Step 1: Verify only pipeline needs the Protocol**

Run:

```powershell
rg -n "class VirtualUserGenerator\\(Protocol\\)|from typing import Protocol" autoresearch/virtual_users
```

Expected before implementation:

```text
autoresearch/virtual_users/pipeline.py
autoresearch/virtual_users/glm_generator.py
```

- [ ] **Step 2: Delete local Protocol**

In `autoresearch/virtual_users/glm_generator.py`, delete:

```python
from typing import Protocol
```

and delete:

```python
class VirtualUserGenerator(Protocol):
    def generate(self, persona: SourcePersona, virtual_user_id: str) -> VirtualUser:
        ...
```

- [ ] **Step 3: Verify only pipeline Protocol remains**

Run:

```powershell
rg -n "class VirtualUserGenerator\\(Protocol\\)|from typing import Protocol" autoresearch/virtual_users
python -m pytest tests/test_virtual_users_glm_generator.py tests/test_virtual_users_pipeline.py -q
```

Expected:

```text
Only autoresearch/virtual_users/pipeline.py has the Protocol.
Tests pass.
```

---

## Task 3: Remove Post-Merge Self-Check

**Files:**
- Modify: `autoresearch/virtual_users/glm_generator.py`
- Modify: `tests/test_virtual_users_glm_generator.py`

- [ ] **Step 1: Keep factual copy coverage in merge test**

Ensure `tests/test_virtual_users_glm_generator.py::test_virtual_user_from_derived_features_copies_source_fields_and_affinity` still asserts:

```python
assert user.source_uuid == persona.uuid
assert user.age == persona.age
assert user.sex == persona.sex
assert user.occupation == persona.occupation
assert user.country == "대한민국"
assert user.source_persona_json["country"] == "대한민국"
```

- [ ] **Step 2: Delete self-check test**

Remove this test from `tests/test_virtual_users_glm_generator.py`:

```python
def test_ensure_source_persona_matches_user_rejects_hallucinated_persona_fields():
    ...
```

Also remove `_ensure_source_persona_matches_user` from the test imports.

- [ ] **Step 3: Delete self-check function and call**

In `autoresearch/virtual_users/glm_generator.py`, delete:

```python
def _ensure_source_persona_matches_user(...):
    ...
```

and delete this block from `_virtual_user_from_derived_features()`:

```python
_ensure_source_persona_matches_user(
    user,
    persona=persona,
    virtual_user_id=virtual_user_id,
)
```

- [ ] **Step 4: Verify**

Run:

```powershell
rg -n "_ensure_source_persona_matches_user" autoresearch tests
python -m pytest tests/test_virtual_users_glm_generator.py -q
```

Expected:

```text
rg exits with no matches
tests pass
```

---

## Task 4: Shrink Duplicate Category Check

**Files:**
- Modify: `autoresearch/virtual_users/categories.py`
- Test: `tests/test_virtual_users_categories.py`

- [ ] **Step 1: Keep duplicate rejection test**

Ensure `tests/test_virtual_users_categories.py` has:

```python
def test_build_category_affinity_rejects_duplicate_primary_categories() -> None:
    allowed = set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES)

    with pytest.raises(ValueError, match="Duplicate"):
        build_category_affinity(
            primary_categories=["Gaming", "Gaming"],
            category_evidence={"Gaming": ["닌텐도 스위치"]},
            allowed_categories=allowed,
        )
```

- [ ] **Step 2: Replace duplicate scan with one condition**

In `autoresearch/virtual_users/categories.py`, replace:

```python
duplicate_categories = [
    category
    for index, category in enumerate(categories)
    if category in categories[:index]
]
if duplicate_categories:
    raise ValueError(f"Duplicate categories: {duplicate_categories}")
```

with:

```python
if len(set(categories)) != len(categories):
    raise ValueError("Duplicate categories are not allowed")
```

- [ ] **Step 3: Verify**

Run:

```powershell
python -m pytest tests/test_virtual_users_categories.py -q
```

Expected:

```text
4 passed
```

---

## Task 5: Final GLM-Only Verification

**Files:**
- No production file changes expected.

- [ ] **Step 1: Verify no legacy provider remnants**

Run:

```powershell
$pattern = 'Gem' + 'ini|gem' + 'ini|use_' + 'gem' + 'ini|request_use_' + 'gem' + 'ini|GEM' + 'INI|GOO' + 'GLE|google-' + 'genai'
rg -n $pattern .
```

Expected:

```text
No matches
```

- [ ] **Step 2: Verify focused virtual user tests**

Run:

```powershell
python -m pytest tests/test_virtual_users_categories.py tests/test_virtual_users_schema.py tests/test_virtual_users_persona_source.py tests/test_virtual_users_interests.py tests/test_virtual_users_glm_generator.py tests/test_virtual_users_pipeline.py -q
```

Expected:

```text
All tests pass
```

- [ ] **Step 3: Verify whole suite**

Run:

```powershell
python -m pytest -q
```

Expected:

```text
78 passed
```

---

## Self-Review

- Spec coverage: all remaining ponytail cleanup items are covered.
- Placeholder scan: no TBD/TODO/implement later steps.
- Type consistency: no new public types introduced; deleted Protocol remains only in `pipeline.py`.
