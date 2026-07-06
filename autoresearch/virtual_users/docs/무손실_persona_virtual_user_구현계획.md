# Persona 원본 전체 보존 Virtual User 구현 계획

> **작업 지침:** 이 문서는 구현자가 순서대로 따라갈 수 있는 체크리스트다. 각 단계는 테스트를 먼저 추가하고, 최소 구현으로 통과시키는 방식으로 진행한다.

**목표:** NVIDIA `Nemotron-Personas-Korea` 원본 persona row 전체를 보존하면서, 최대 100명의 virtual user를 GLM 기반으로 생성한다. 생성 결과는 원본 근거를 추적할 수 있어야 하고, Kaggle YouTube 영상 카테고리와 매칭 가능한 형태여야 한다.

**핵심 방향:** 정규화는 유지하되, 정보 축약이 아니라 **무손실에 가까운 source contract**로 바꾼다. GLM은 취향/관심사/카테고리 근거 같은 derived feature만 생성하고, 나이/성별/학력/직업/지역 같은 factual field는 코드가 원본에서 복사한다.

**사용 기술:** Python, Pydantic v2, PyArrow Parquet, DuckDB, OpenAI-compatible GLM client, pytest.

---

## 설계 원칙

- 생성 규모는 최대 100건이므로 저장 용량보다 원본 추적성과 품질을 우선한다.
- 정규화는 컬럼 삭제가 아니다. 타입 안정화, 값 표준화, 누락값 처리, 검증, provenance 부여만 담당한다.
- raw persona row는 원본 그대로 저장한다.
- normalized source persona는 원본 26개 컬럼을 모두 보존한다.
- GLM은 factual field를 다시 작성하지 않는다.
- GLM은 `persona_summary`, keyword group, category candidate, category evidence, viewing tendency만 생성한다.
- `category_affinity` 숫자는 GLM이 임의 생성하지 않고, 코드가 category 순위와 evidence 개수를 기반으로 deterministic하게 계산한다.
- `primary_categories`와 `category_affinity` key는 Kaggle trending YouTube dataset의 `video_category` vocabulary 안에서만 허용한다.

## 목표 데이터 흐름

```text
NVIDIA parquet raw row
  -> raw persona snapshot JSONL
  -> 모든 원본 컬럼을 보존하는 SourcePersona
  -> 전체 source payload + Kaggle category vocab을 GLM prompt에 전달
  -> GLM이 DerivedVirtualUserFeatures 생성
  -> 코드가 factual field 복사 + category_affinity 계산
  -> VirtualUser
  -> parquet batch output + warehouse JSONL
```

## SourcePersona 필수 보존 컬럼

아래 raw source 컬럼은 모두 보존한다.

```text
uuid
professional_persona
sports_persona
arts_persona
travel_persona
culinary_persona
family_persona
persona
cultural_background
skills_and_expertise
skills_and_expertise_list
hobbies_and_interests
hobbies_and_interests_list
career_goals_and_ambitions
sex
age
marital_status
military_status
family_type
housing_type
education_level
bachelors_field
occupation
district
province
country
```

정규화 helper field는 아래를 추가한다.

```text
sex_normalized
country_code
locale
source_text
source_hash
raw_payload
```

## Virtual User 목표 컬럼

warehouse row는 아래 필드를 갖도록 한다.

```text
user_id
source_uuid
source_dataset
source_hash
country
locale
age
sex
marital_status
military_status
family_type
housing_type
education_level
bachelors_field
occupation
province
district
persona_summary
hobby_keywords
interest_keywords
lifestyle_keywords
food_keywords
travel_keywords
career_keywords
family_context_keywords
primary_categories
category_evidence
category_affinity
shorts_affinity
longform_affinity
trend_sensitivity
comment_propensity
watch_time_band
source_persona_json
schema_version
prompt_version
llm_model
generated_at
```

`source_persona_json`에는 normalized source 전체와 `raw_payload`를 포함한다. 최대 100건이면 저장 부담보다 감사 가능성이 더 중요하다.

---

## Task 1: SourcePersona 스키마 확장

**파일:**
- 수정: `autoresearch/virtual_users/schema.py`
- 수정: `tests/test_virtual_users_schema.py`
- 수정: `tests/test_virtual_users_persona_source.py`

- [ ] **Step 1: 전체 source 컬럼 보존 실패 테스트 추가**

`SourcePersona`가 raw NVIDIA 컬럼 전체와 helper field를 받을 수 있는지 테스트한다.

```python
def test_source_persona_preserves_full_raw_persona_contract():
    persona = SourcePersona(
        uuid="p-001",
        professional_persona="책 큐레이션을 잘한다.",
        sports_persona="숲길을 천천히 걷는다.",
        arts_persona="동물의 숲과 LP 음악을 좋아한다.",
        travel_persona="조용한 해변과 골목 카페를 찾는다.",
        culinary_persona="마라탕과 꿔바로우를 주문한다.",
        family_persona="혼자 살지만 가족과 정서적 유대가 있다.",
        persona="제주의 작은 서점에서 일하는 22세 여성.",
        cultural_background="제주시 아파트 단지에서 성장했다.",
        skills_and_expertise="독립출판물 큐레이션",
        skills_and_expertise_list=["독립출판물 큐레이션", "고객 응대"],
        hobbies_and_interests="사려니숲길 산책, 닌텐도 스위치",
        hobbies_and_interests_list=["사려니숲길 산책", "닌텐도 스위치"],
        career_goals_and_ambitions="작은 교육 공방을 운영하고 싶어 한다.",
        sex="female",
        sex_normalized="female",
        age=22,
        marital_status="미혼",
        military_status="비현역",
        family_type="혼자 거주",
        housing_type="아파트",
        education_level="4년제 대학교",
        bachelors_field="교육",
        occupation="서적·문구 및 음반 판매원",
        district="제주-제주시",
        province="제주",
        country="대한민국",
        country_code="KR",
        locale="ko-KR",
        source_text="제주의 작은 서점에서 일하는 22세 여성.",
        source_hash="hash",
        raw_payload={"uuid": "p-001"},
    )

    assert persona.career_goals_and_ambitions == "작은 교육 공방을 운영하고 싶어 한다."
    assert persona.education_level == "4년제 대학교"
    assert persona.raw_payload["uuid"] == "p-001"
```

- [ ] **Step 2: 테스트 실패 확인**

```powershell
python -m pytest tests/test_virtual_users_schema.py::test_source_persona_preserves_full_raw_persona_contract -q
```

예상: 아직 스키마에 없는 필드 때문에 실패한다.

- [ ] **Step 3: `SourcePersona` 확장**

`SourcePersona`에 원본 컬럼 전체와 helper field를 추가한다.

```python
class SourcePersona(BaseModel):
    uuid: str
    professional_persona: str = ""
    sports_persona: str = ""
    arts_persona: str = ""
    travel_persona: str = ""
    culinary_persona: str = ""
    family_persona: str = ""
    persona: str = ""
    cultural_background: str = ""
    skills_and_expertise: str = ""
    skills_and_expertise_list: list[str] = Field(default_factory=list)
    hobbies_and_interests: str = ""
    hobbies_and_interests_list: list[str] = Field(default_factory=list)
    career_goals_and_ambitions: str = ""
    sex: Literal["male", "female"]
    sex_normalized: Literal["male", "female"] | None = None
    age: int
    marital_status: str = ""
    military_status: str = ""
    family_type: str = ""
    housing_type: str = ""
    education_level: str = ""
    bachelors_field: str = ""
    occupation: str = ""
    district: str = ""
    province: str = ""
    country: str = ""
    country_code: str = SOURCE_COUNTRY
    locale: str = SOURCE_LOCALE
    source_text: str = ""
    source_hash: str = ""
    raw_payload: dict[str, object] = Field(default_factory=dict)
```

- [ ] **Step 4: source 관련 테스트 실행**

```powershell
python -m pytest tests/test_virtual_users_schema.py tests/test_virtual_users_persona_source.py -q
```

예상: 기존 fixture 호출부에서 기본값 부족 문제가 있으면 이 단계에서 드러난다.

---

## Task 2: Persona 정규화를 무손실에 가깝게 변경

**파일:**
- 수정: `autoresearch/virtual_users/persona_source.py`
- 수정: `tests/test_virtual_users_persona_source.py`

- [ ] **Step 1: raw record 전체 보존 테스트 추가**

```python
def test_source_persona_from_record_preserves_all_raw_columns():
    raw = {
        "uuid": "raw-001",
        "professional_persona": "책을 큐레이션한다.",
        "sports_persona": "숲길을 걷는다.",
        "arts_persona": "동물의 숲과 LP 음악을 좋아한다.",
        "travel_persona": "조용한 해변을 찾는다.",
        "culinary_persona": "마라탕을 주문한다.",
        "family_persona": "혼자 거주한다.",
        "persona": "제주의 작은 서점에서 일한다.",
        "cultural_background": "제주 지역 맥락.",
        "skills_and_expertise": "책 큐레이션",
        "skills_and_expertise_list": "['책 큐레이션', '고객 응대']",
        "hobbies_and_interests": "사려니숲길 산책, 닌텐도 스위치",
        "hobbies_and_interests_list": "['사려니숲길 산책', '닌텐도 스위치']",
        "career_goals_and_ambitions": "교육 공방을 열고 싶어 한다.",
        "sex": "여자",
        "age": 22,
        "marital_status": "미혼",
        "military_status": "비현역",
        "family_type": "혼자 거주",
        "housing_type": "아파트",
        "education_level": "4년제 대학교",
        "bachelors_field": "교육",
        "occupation": "서적·문구 및 음반 판매원",
        "district": "제주-제주시",
        "province": "제주",
        "country": "대한민국",
    }

    persona = source_persona_from_record(raw)

    assert persona.sex == "female"
    assert persona.sex_normalized == "female"
    assert persona.country == "대한민국"
    assert persona.country_code == "KR"
    assert persona.career_goals_and_ambitions == "교육 공방을 열고 싶어 한다."
    assert persona.raw_payload["uuid"] == "raw-001"
    assert persona.source_hash
    assert "교육 공방을 열고 싶어 한다." in persona.source_text
```

- [ ] **Step 2: 테스트 실패 확인**

```powershell
python -m pytest tests/test_virtual_users_persona_source.py::test_source_persona_from_record_preserves_all_raw_columns -q
```

- [ ] **Step 3: 안정적인 helper 구현**

list-like string, source text, hash를 안정적으로 처리한다.

```python
import ast
import hashlib

def _as_text_list(record: dict[str, Any], key: str) -> list[str]:
    value = record.get(key, [])
    if value is None:
        return []
    if isinstance(value, list):
        return [str(item).strip() for item in value if str(item).strip()]
    text = str(value).strip()
    if not text:
        return []
    try:
        parsed = ast.literal_eval(text)
    except (SyntaxError, ValueError):
        parsed = None
    if isinstance(parsed, list):
        return [str(item).strip() for item in parsed if str(item).strip()]
    return [part.strip().strip("'\"") for part in text.split(",") if part.strip()]

def _source_text(record: dict[str, Any]) -> str:
    keys = [
        "persona",
        "professional_persona",
        "sports_persona",
        "arts_persona",
        "travel_persona",
        "culinary_persona",
        "family_persona",
        "cultural_background",
        "skills_and_expertise",
        "hobbies_and_interests",
        "career_goals_and_ambitions",
        "marital_status",
        "family_type",
        "housing_type",
        "education_level",
        "bachelors_field",
        "occupation",
        "district",
        "province",
    ]
    return "\n".join(_as_text(record, key) for key in keys if _as_text(record, key))

def _source_hash(record: dict[str, Any]) -> str:
    payload = json.dumps(record, ensure_ascii=False, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()
```

- [ ] **Step 4: `source_persona_from_record()` 전체 매핑**

모든 raw 컬럼을 `SourcePersona`에 넣고, `sex`와 `sex_normalized`에는 정규화된 성별을 넣는다. `source_text`, `source_hash`, `raw_payload`도 함께 채운다.

- [ ] **Step 5: persona source 테스트 실행**

```powershell
python -m pytest tests/test_virtual_users_persona_source.py -q
```

---

## Task 3: GLM 출력은 derived feature만 받도록 분리

**파일:**
- 수정: `autoresearch/virtual_users/schema.py`
- 수정: `autoresearch/virtual_users/glm_generator.py`
- 수정: `tests/test_virtual_users_glm_generator.py`

- [ ] **Step 1: `DerivedVirtualUserFeatures` 추가**

GLM이 생성해야 하는 필드만 별도 모델로 분리한다.

```python
class DerivedVirtualUserFeatures(BaseModel):
    persona_summary: str
    hobby_keywords: list[str] = Field(default_factory=list)
    interest_keywords: list[str] = Field(default_factory=list)
    lifestyle_keywords: list[str] = Field(default_factory=list)
    food_keywords: list[str] = Field(default_factory=list)
    travel_keywords: list[str] = Field(default_factory=list)
    career_keywords: list[str] = Field(default_factory=list)
    family_context_keywords: list[str] = Field(default_factory=list)
    primary_categories: list[str] = Field(min_length=1, max_length=5)
    category_evidence: dict[str, list[str]] = Field(default_factory=dict)
    shorts_affinity: float = Field(ge=0.0, le=1.0)
    longform_affinity: float = Field(ge=0.0, le=1.0)
    trend_sensitivity: float = Field(ge=0.0, le=1.0)
    comment_propensity: float = Field(ge=0.0, le=1.0)
    watch_time_band: Literal["morning", "afternoon", "evening", "night", "mixed"]
```

- [ ] **Step 2: GLM prompt contract 변경**

prompt에는 normalized source 전체를 보여주되, 출력 JSON에는 derived feature만 요구한다.

```json
{
  "persona_summary": "한 문장 요약",
  "hobby_keywords": ["..."],
  "interest_keywords": ["..."],
  "lifestyle_keywords": ["..."],
  "food_keywords": ["..."],
  "travel_keywords": ["..."],
  "career_keywords": ["..."],
  "family_context_keywords": ["..."],
  "primary_categories": ["..."],
  "category_evidence": {
    "Music": ["근거"]
  },
  "shorts_affinity": 0.0,
  "longform_affinity": 0.0,
  "trend_sensitivity": 0.0,
  "comment_propensity": 0.0,
  "watch_time_band": "night"
}
```

- [ ] **Step 3: system harness 추가**

```python
GLM_SYSTEM_HARNESS = """너는 virtual user feature extractor다.
source data의 demographic/factual 필드는 절대 변경하지 마라.
모든 persona 컬럼을 근거로 관심사와 취향을 추론하라.
출력은 지정된 derived JSON schema만 허용한다.
없는 정보를 만들지 말고 source에서 추론 가능한 수준만 생성하라.
category는 제공된 allowed category vocabulary 안에서만 선택하라.
generation_meta는 만들지 마라.
"""
```

- [ ] **Step 4: 응답 파싱 변경**

`VirtualUser.model_validate(payload)`로 바로 검증하지 말고 `DerivedVirtualUserFeatures.model_validate(payload)`로 검증한다.

- [ ] **Step 5: GLM generator 테스트 실행**

```powershell
python -m pytest tests/test_virtual_users_glm_generator.py -q
```

---

## Task 4: Kaggle category vocabulary 검증 추가

**파일:**
- 생성: `autoresearch/virtual_users/categories.py`
- 수정: `tests/test_virtual_users_glm_generator.py`
- 수정: `tests/test_virtual_users_schema.py`

- [ ] **Step 1: 허용 category helper 생성**

Kaggle `video_category` 기준 category를 기본 vocabulary로 둔다.

```python
DEFAULT_KAGGLE_YOUTUBE_CATEGORIES = [
    "Film & Animation",
    "Autos & Vehicles",
    "Music",
    "Pets & Animals",
    "Sports",
    "Travel & Events",
    "Gaming",
    "People & Blogs",
    "Comedy",
    "Entertainment",
    "News & Politics",
    "Howto & Style",
    "Education",
    "Science & Technology",
    "Nonprofits & Activism",
]

def validate_categories(categories: list[str], allowed: set[str]) -> list[str]:
    invalid = [category for category in categories if category not in allowed]
    if invalid:
        raise ValueError(f"Unknown Kaggle YouTube categories: {invalid}")
    return categories
```

- [ ] **Step 2: 검증 테스트 추가**

```python
def test_validate_categories_rejects_non_kaggle_category():
    allowed = set(DEFAULT_KAGGLE_YOUTUBE_CATEGORIES)

    with pytest.raises(ValueError, match="Travel"):
        validate_categories(["Gaming", "Travel"], allowed)

    assert validate_categories(["Gaming", "Travel & Events"], allowed) == [
        "Gaming",
        "Travel & Events",
    ]
```

- [ ] **Step 3: GLM prompt에 allowed vocabulary 명시**

```text
Allowed category vocabulary:
- Film & Animation
- Autos & Vehicles
- Music
- Pets & Animals
- Sports
- Travel & Events
- Gaming
- People & Blogs
- Comedy
- Entertainment
- News & Politics
- Howto & Style
- Education
- Science & Technology
- Nonprofits & Activism
```

---

## Task 5: category_affinity를 코드에서 계산

**파일:**
- 생성 또는 수정: `autoresearch/virtual_users/categories.py`
- 수정: `autoresearch/virtual_users/glm_generator.py`
- 수정: `tests/test_virtual_users_glm_generator.py`

- [ ] **Step 1: deterministic scoring 테스트 추가**

```python
def test_build_category_affinity_scores_rank_and_evidence():
    scores = build_category_affinity(
        primary_categories=["Gaming", "Entertainment", "Music"],
        category_evidence={
            "Gaming": ["닌텐도 스위치", "동물의 숲"],
            "Entertainment": ["넷플릭스"],
            "Music": ["LP"],
        },
        allowed_categories={"Gaming", "Entertainment", "Music"},
    )

    assert scores["Gaming"] == 0.91
    assert scores["Entertainment"] == 0.75
    assert scores["Music"] == 0.63
```

- [ ] **Step 2: scoring 함수 구현**

```python
RANK_BASE = {
    1: 0.85,
    2: 0.72,
    3: 0.60,
    4: 0.50,
    5: 0.42,
}

def build_category_affinity(
    primary_categories: list[str],
    category_evidence: dict[str, list[str]],
    allowed_categories: set[str],
) -> dict[str, float]:
    validate_categories(primary_categories, allowed_categories)
    scores: dict[str, float] = {}
    for rank, category in enumerate(primary_categories, start=1):
        evidence_count = len(category_evidence.get(category, []))
        evidence_boost = min(0.10, evidence_count * 0.03)
        scores[category] = round(min(0.95, RANK_BASE[rank] + evidence_boost), 2)
    return scores
```

- [ ] **Step 3: source + derived merge 함수 추가**

`SourcePersona`에서 factual field를 복사하고, `DerivedVirtualUserFeatures`에서 preference field를 넣고, `category_affinity`는 위 함수 결과를 넣어 `VirtualUser`를 만든다.

---

## Task 6: VirtualUser와 출력 schema 확장

**파일:**
- 수정: `autoresearch/virtual_users/schema.py`
- 수정: `autoresearch/virtual_users/pipeline.py`
- 수정: `tests/test_virtual_users_schema.py`
- 수정: `tests/test_virtual_users_pipeline.py`

- [ ] **Step 1: warehouse row shape 테스트 추가**

```python
def test_virtual_user_exports_lossless_warehouse_row():
    row = user.to_warehouse_row()

    assert row["source_hash"]
    assert row["education_level"] == "4년제 대학교"
    assert row["career_keywords"] == ["교육 공방"]
    assert row["category_evidence"]["Gaming"] == ["닌텐도 스위치"]
    assert row["source_persona_json"]["uuid"] == user.source_uuid
```

- [ ] **Step 2: `VirtualUser` 필드 확장**

아래 필드를 추가한다.

```text
source_hash
marital_status
military_status
family_type
housing_type
education_level
bachelors_field
lifestyle_keywords
food_keywords
travel_keywords
career_keywords
family_context_keywords
category_evidence
source_persona_json
```

- [ ] **Step 3: parquet schema 확장**

`VIRTUAL_USERS_PARQUET_SCHEMA`에도 같은 필드를 추가한다.

권장 타입:

```text
source/factual field: pa.string()
keyword list: pa.list_(pa.string())
category_evidence: JSON string 우선
source_persona_json: JSON string 우선
```

첫 구현에서는 DuckDB QA 편의성을 위해 `category_evidence`와 `source_persona_json`은 JSON string으로 저장하는 쪽을 추천한다.

- [ ] **Step 4: pipeline 테스트 실행**

```powershell
python -m pytest tests/test_virtual_users_schema.py tests/test_virtual_users_pipeline.py -q
```

---

## Task 7: 2건/100건 QA 명령 추가

**파일:**
- 수정: `autoresearch/virtual_users/docs/버추얼유저_생성_구현계획.md`
- 또는 생성: `autoresearch/virtual_users/docs/lossless_persona_virtual_user_qa.md`

- [ ] **Step 1: 2건 smoke test 명령 문서화**

로컬 parquet에서 20대 여성 1명, 남성 1명을 읽고 GLM 결과를 생성하는 명령을 문서화한다.

- [ ] **Step 2: 100건 생성 명령 문서화**

기본값:

```text
male_count=50
female_count=50
max_concurrency=1
```

출력:

```text
asset/virtual_user/virtual_users_20s_100.parquet
data/raw/personas/nvidia_personas_kr_100.jsonl
data/generated/virtual_users_kr.jsonl
```

- [ ] **Step 3: 검증 쿼리 문서화**

```sql
SELECT llm_model, COUNT(*)
FROM read_parquet('asset/virtual_user/virtual_users_20s_100.parquet')
GROUP BY llm_model;
```

```sql
SELECT COUNT(*)
FROM read_parquet('asset/virtual_user/virtual_users_20s_100.parquet')
WHERE source_persona_json IS NULL OR source_persona_json = '';
```

---

## Task 8: 최종 검증

**파일:**
- 새 파일 없음

- [ ] **Step 1: 집중 테스트 실행**

```powershell
python -m pytest tests/test_virtual_users_schema.py tests/test_virtual_users_persona_source.py tests/test_virtual_users_glm_generator.py tests/test_virtual_users_pipeline.py -q
```

- [ ] **Step 2: 알려진 샘플 2건 생성**

사용할 source UUID:

```text
00004369cb9642318291395288a0de5d
0003ee56196a4bad9dc5647fb5d15dbf
```

기대 결과:

```text
source factual field가 모두 보존된다.
source_persona_json이 비어 있지 않다.
llm_model="glm-5.2"가 찍힌다.
primary_categories는 Kaggle category name만 포함한다.
category_affinity는 코드 계산값이다.
```

- [ ] **Step 3: 최대 100건 생성**

`ZAI_API_KEY`가 shell에 설정된 상태에서 100건 생성 명령을 실행한다.

기대 결과:

```text
parquet_rows=100
raw_snapshot_rows=100
warehouse_jsonl_lines=100
invalid_category_count=0
missing_source_json_count=0
```

---

## 구현 전 결정할 사항

- `category_evidence`를 nested parquet로 저장할지 JSON string으로 저장할지 결정해야 한다. 1차 구현은 JSON string을 추천한다.
- `source_persona_json`에는 normalized source 전체와 `raw_payload`를 함께 넣는 것을 추천한다.
- Kaggle category vocabulary는 기본 hardcoded list를 두고, 나중에 local Kaggle parquet에서 override할 수 있게 만드는 것을 추천한다.
- `shorts_affinity`, `longform_affinity`, `trend_sensitivity`, `comment_propensity`는 일단 GLM이 만들고, event log가 생긴 뒤 deterministic 또는 learned feature로 바꾸는 것을 추천한다.

## 완료 기준

- GLM 입력에 원본 persona 전체 의미가 들어간다.
- source normalization 과정에서 raw persona 컬럼이 조용히 삭제되지 않는다.
- virtual user output에서 원본 source와 생성 feature를 모두 추적할 수 있다.
- factual field는 GLM 응답을 믿지 않고 source에서 복사한다.
- `primary_categories`와 `category_affinity`는 Kaggle YouTube category name만 사용한다.
- `category_affinity`는 동일한 category ranking/evidence에 대해 항상 같은 값이 나온다.
- 2건 local QA와 100건 생성 QA가 모두 통과한다.
