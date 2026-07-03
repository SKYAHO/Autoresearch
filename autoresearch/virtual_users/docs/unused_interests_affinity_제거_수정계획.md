# Unused Interests Affinity 제거 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** `interests.py`의 미사용 `VirtualUserInterests.category_affinity`와 `_category_affinity()`를 제거해 affinity 계산 경로를 하나로 통일한다.

**Architecture:** 최종 `VirtualUser.category_affinity` 컬럼은 유지한다. 이 값은 `glm_generator._virtual_user_from_derived_features()`가 `categories.py::build_category_affinity()`로 계산하며, 향후 `virtual_user_action_log` 생성에서 클릭 확률 feature로 사용한다. 제거 대상은 최종 output에 소비되지 않는 `interests.py` 내부 fallback affinity 계산뿐이다.

**Tech Stack:** Python, dataclasses, pytest.

---

## 현재 상태와 결정

현재 affinity 계산 경로는 두 개처럼 보인다.

```text
1. interests.py::_category_affinity()
   - persona text keyword 기반
   - VirtualUserInterests.category_affinity에 저장
   - 현재 최종 VirtualUser output에서 소비되지 않음

2. categories.py::build_category_affinity()
   - GLM primary_categories ranking + category_evidence 기반
   - VirtualUser.category_affinity에 저장
   - parquet/warehouse JSONL에 실제 출력됨
```

`virtual_user_action_log` 생성에 필요한 것은 2번의 최종 output 컬럼이다.

```text
VirtualUser.category_affinity
```

따라서 `category_affinity` 개념과 output 컬럼은 유지한다. 다만 `interests.py`의 미사용 계산은 제거한다.

최종 계약:

```text
interests.py:
  hobby_keywords, interest_keywords만 담당

categories.py:
  allowed category validation
  deterministic category_affinity 계산 담당

glm_generator.py:
  derived features + SourcePersona 병합 중 categories.py의 affinity 계산 사용
```

Claude review 답변 요지:

```text
category_affinity 자체는 action log의 클릭 확률 계산 feature로 필요하므로 VirtualUser output에는 유지합니다. 다만 interests.py의 VirtualUserInterests.category_affinity는 실제 소비처가 없고 categories.py::build_category_affinity와 계산 경로가 중복되어 혼동을 만들기 때문에 제거하겠습니다.
```

## File Map

- Modify: `autoresearch/virtual_users/interests.py`
  - `DEFAULT_KAGGLE_YOUTUBE_CATEGORIES` import 제거
  - `VirtualUserInterests.category_affinity` 제거
  - `CATEGORY_KEYWORDS` 제거
  - `_category_affinity()` 제거
  - `extract_virtual_user_interests()` 반환값에서 affinity 제거
- Modify: `tests/test_virtual_users_interests.py`
  - `CATEGORY_KEYWORDS` import 제거
  - `test_interest_category_keywords_use_kaggle_vocab` 제거 또는 대체
  - keyword extraction 테스트 유지
- Modify: `tests/test_virtual_users_glm_generator.py`
  - `RuleBasedVirtualUserGenerator`가 계속 valid `VirtualUser.category_affinity`를 만드는지 유지 확인
- Modify: `autoresearch/virtual_users/docs/무손실_persona_virtual_user_구현계획.md`
  - affinity source of truth가 `categories.py::build_category_affinity()`임을 명시
- Modify: `autoresearch/virtual_users/docs/subagent_구현_실행계획.md`
  - interests.py가 affinity를 계산한다는 뉘앙스가 있으면 제거

---

## Task 1: tests에서 interests affinity 의존 제거

**Files:**
- Modify: `tests/test_virtual_users_interests.py`

- [ ] **Step 1: Remove unused imports**

`tests/test_virtual_users_interests.py`에서 아래 import를 제거한다.

```python
from autoresearch.virtual_users.categories import DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
```

아래 import 목록에서 `CATEGORY_KEYWORDS`를 제거한다.

```python
from autoresearch.virtual_users.interests import (
    CATEGORY_KEYWORDS,
    extract_interest_keywords,
)
```

수정 후 import는 아래처럼 둔다.

```python
from autoresearch.virtual_users.interests import extract_interest_keywords
from autoresearch.virtual_users.schema import SourcePersona
```

- [ ] **Step 2: Remove old category keyword vocab test**

아래 테스트를 제거한다.

```python
def test_interest_category_keywords_use_kaggle_vocab():
    assert set(CATEGORY_KEYWORDS).issubset(set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES))
    assert "Travel & Events" in DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
```

이 테스트는 `interests.py`가 category affinity를 계산하던 시절의 coupling을 확인한다. source of truth는 이제 `categories.py` 테스트가 담당한다.

- [ ] **Step 3: Run interests tests**

```powershell
python -m pytest tests/test_virtual_users_interests.py -q
```

Expected:

```text
2 passed
```

- [ ] **Step 4: Commit test cleanup**

```powershell
git add tests/test_virtual_users_interests.py
git commit -m "test: remove unused interests affinity expectations"
```

---

## Task 2: interests.py에서 미사용 affinity 제거

**Files:**
- Modify: `autoresearch/virtual_users/interests.py`

- [ ] **Step 1: Remove category import**

`autoresearch/virtual_users/interests.py`에서 아래 import를 제거한다.

```python
from autoresearch.virtual_users.categories import DEFAULT_KAGGLE_YOUTUBE_CATEGORIES
```

- [ ] **Step 2: Remove dataclass field**

`VirtualUserInterests`를 아래처럼 바꾼다.

```python
@dataclass(frozen=True)
class VirtualUserInterests:
    hobby_keywords: list[str]
    interest_keywords: list[str]
```

- [ ] **Step 3: Remove CATEGORY_KEYWORDS**

아래 dict 전체를 제거한다.

```python
CATEGORY_KEYWORDS: dict[str, tuple[str, ...]] = {
    "Gaming": ("game", "gaming", "esports"),
    "Music": ("music", "song", "artist", "playlist"),
    "Entertainment": ("creator", "short-form", "video", "entertainment", "comedy"),
    "Education": ("study", "learning", "education", "learner"),
    "News & Politics": ("news", "politics", "current affairs"),
    "Sports": ("sports", "football", "baseball", "basketball"),
    "Science & Technology": ("technology", "tech", "developer", "software", "coding"),
    "Howto & Style": ("beauty", "makeup", "fashion", "style", "cooking", "recipe"),
    "People & Blogs": ("family", "home", "daily", "lifestyle", "travel", "cafe"),
    "Comedy": ("comedy", "funny", "humor"),
}
```

- [ ] **Step 4: Remove _category_affinity()**

아래 함수 전체를 제거한다.

```python
def _category_affinity(persona: SourcePersona) -> dict[str, float]:
    text = _persona_text(persona)
    scores: dict[str, float] = {}
    for category in DEFAULT_KAGGLE_YOUTUBE_CATEGORIES:
        aliases = CATEGORY_KEYWORDS.get(category, ())
        hits = sum(1 for alias in aliases if alias in text)
        if hits:
            scores[category] = min(0.95, 0.45 + hits * 0.15)

    if not scores:
        scores["Entertainment"] = 0.5
        scores["People & Blogs"] = 0.45
    return scores
```

- [ ] **Step 5: Update extract_virtual_user_interests()**

아래 구현을:

```python
def extract_virtual_user_interests(persona: SourcePersona) -> VirtualUserInterests:
    """Build deterministic fallback interest features for GLM output rows."""

    return VirtualUserInterests(
        hobby_keywords=_extract_hobby_keywords(persona),
        interest_keywords=extract_interest_keywords(persona),
        category_affinity=_category_affinity(persona),
    )
```

아래처럼 바꾼다.

```python
def extract_virtual_user_interests(persona: SourcePersona) -> VirtualUserInterests:
    """Build deterministic fallback keyword features for fixture generation."""

    return VirtualUserInterests(
        hobby_keywords=_extract_hobby_keywords(persona),
        interest_keywords=extract_interest_keywords(persona),
    )
```

- [ ] **Step 6: Verify no removed symbols remain**

```powershell
rg -n "CATEGORY_KEYWORDS|_category_affinity|VirtualUserInterests\\(.*category_affinity|category_affinity" autoresearch/virtual_users/interests.py tests/test_virtual_users_interests.py
```

Expected:

```text
No matches.
```

- [ ] **Step 7: Run interests and generator tests**

```powershell
python -m pytest tests/test_virtual_users_interests.py tests/test_virtual_users_glm_generator.py -q
```

Expected:

```text
All tests pass.
```

- [ ] **Step 8: Commit runtime cleanup**

```powershell
git add autoresearch/virtual_users/interests.py tests/test_virtual_users_interests.py
git commit -m "refactor: remove unused interests affinity"
```

---

## Task 3: final affinity 경로 보호 테스트 확인

**Files:**
- Modify: `tests/test_virtual_users_glm_generator.py` only if assertion is missing
- Modify: `tests/test_virtual_users_pipeline.py` only if assertion is missing

- [ ] **Step 1: Confirm merge test protects final affinity**

`tests/test_virtual_users_glm_generator.py::test_virtual_user_from_derived_features_copies_source_fields_and_affinity`에 아래 assertion이 있는지 확인한다.

```python
assert user.category_affinity == {"Gaming": 0.91, "Music": 0.75}
```

없으면 추가한다.

- [ ] **Step 2: Confirm rule-based generator still emits final affinity**

`tests/test_virtual_users_glm_generator.py::test_rule_based_generator_produces_valid_schema_without_api_call`에 아래 assertion이 있는지 확인한다.

```python
assert user.category_affinity
```

없으면 추가한다.

- [ ] **Step 3: Confirm parquet still writes final affinity**

`tests/test_virtual_users_pipeline.py::test_generate_virtual_user_batch_writes_expected_100_user_parquet`에 아래 assertion이 있는지 확인한다.

```python
assert rows[0]["category_affinity"]
```

없으면 추가한다.

- [ ] **Step 4: Run protection tests**

```powershell
python -m pytest tests/test_virtual_users_glm_generator.py::test_virtual_user_from_derived_features_copies_source_fields_and_affinity tests/test_virtual_users_glm_generator.py::test_rule_based_generator_produces_valid_schema_without_api_call tests/test_virtual_users_pipeline.py::test_generate_virtual_user_batch_writes_expected_100_user_parquet -q
```

Expected:

```text
3 passed
```

- [ ] **Step 5: Commit only if assertions were added**

If Step 1-3 already had all assertions, skip this commit. If any assertion was added, run:

```powershell
git add tests/test_virtual_users_glm_generator.py tests/test_virtual_users_pipeline.py
git commit -m "test: protect final virtual user affinity output"
```

---

## Task 4: 문서에서 affinity source of truth 정리

**Files:**
- Modify: `autoresearch/virtual_users/docs/무손실_persona_virtual_user_구현계획.md`
- Modify: `autoresearch/virtual_users/docs/subagent_구현_실행계획.md`

- [ ] **Step 1: Update implementation plan affinity wording**

`autoresearch/virtual_users/docs/무손실_persona_virtual_user_구현계획.md`에서 아래 의미를 유지하는 문장을 확인한다.

```text
`category_affinity` 숫자는 GLM이 임의 생성하지 않고, 코드가 category 순위와 evidence 개수를 기반으로 deterministic하게 계산한다.
```

문장이 없다면 설계 원칙 섹션에 추가한다.

- [ ] **Step 2: Add explicit source of truth sentence**

같은 문서에 아래 문장을 추가한다.

```text
`category_affinity`의 단일 source of truth는 `autoresearch/virtual_users/categories.py::build_category_affinity()`다. `interests.py`는 hobby/interest keyword extraction만 담당한다.
```

- [ ] **Step 3: Update subagent plan wording**

`autoresearch/virtual_users/docs/subagent_구현_실행계획.md`에서 Subagent A 설명이 아래 의미를 포함하는지 확인한다.

```text
Kaggle YouTube category vocabulary와 deterministic affinity 계산을 독립 모듈로 만든다.
```

그리고 `interests.py`가 affinity를 계산한다는 문장이 있으면 제거한다.

- [ ] **Step 4: Verify docs mention final affinity path**

```powershell
Select-String -Path "autoresearch\\virtual_users\\docs\\무손실_persona_virtual_user_구현계획.md" -Pattern "build_category_affinity|source of truth"
```

Expected:

```text
At least one line mentioning build_category_affinity or source of truth.
```

- [ ] **Step 5: Commit docs**

```powershell
git add autoresearch/virtual_users/docs/무손실_persona_virtual_user_구현계획.md autoresearch/virtual_users/docs/subagent_구현_실행계획.md
git commit -m "docs: clarify virtual user affinity source"
```

---

## Task 5: 최종 검증과 PR 리뷰 답변

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
맞습니다. action log 생성에 필요한 최종 category_affinity 컬럼은 유지하되, interests.py의 VirtualUserInterests.category_affinity는 실제 소비되지 않고 categories.py::build_category_affinity와 계산 경로가 중복되어 제거했습니다. 이제 interests.py는 keyword extraction만 담당하고, 최종 affinity의 단일 source of truth는 categories.py::build_category_affinity입니다.
```

- [ ] **Step 4: Push branch**

```powershell
git push
```

Expected:

```text
Existing PR #43 updates with the cleanup.
```

---

## Self-Review

- Spec coverage: Claude review가 지적한 미사용 `VirtualUserInterests.category_affinity`와 `_category_affinity()` 제거를 코드, 테스트, 문서로 나눴다.
- Final output preservation: `VirtualUser.category_affinity`와 `categories.py::build_category_affinity()`는 유지한다고 명시했다.
- Future action log fit: action log 클릭 확률 feature는 final output의 `VirtualUser.category_affinity`를 사용하도록 정리했다.
- Type consistency: `VirtualUserInterests`는 `hobby_keywords`, `interest_keywords`만 갖도록 모든 task에서 동일하게 사용했다.
- Placeholder scan: 실행자가 채워야 하는 빈 단계 없이 명령과 기대 결과를 포함했다.
