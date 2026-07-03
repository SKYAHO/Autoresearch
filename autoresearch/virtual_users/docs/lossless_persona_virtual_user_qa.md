# 무손실 Persona Virtual User QA 가이드

이 문서는 로컬에 받은 `Nemotron-Personas-Korea/data/*.parquet`에서 20대 남녀 persona를 읽고, GLM 기반 virtual user parquet/JSONL 산출물을 검증하는 절차다.

## 전제 조건

- 현재 브랜치: `42-feat-glm-기반-virtual-user-parquet-생성-파이프라인`
- 로컬 원본 parquet: `Nemotron-Personas-Korea/data/*.parquet`
- PowerShell User env 또는 현재 shell env에 `ZAI_API_KEY` 존재
- DuckDB, PyArrow, datasets, openai Python package 사용 가능

PowerShell에서 User env에 저장한 API key를 현재 shell에 주입한다.

```powershell
$env:ZAI_API_KEY = [Environment]::GetEnvironmentVariable("ZAI_API_KEY", "User")
if (-not $env:ZAI_API_KEY) { throw "ZAI_API_KEY가 없습니다." }
```

## 1. 관련 테스트

```powershell
python -m pytest tests/test_virtual_users_categories.py tests/test_virtual_users_schema.py tests/test_virtual_users_persona_source.py tests/test_virtual_users_glm_generator.py tests/test_virtual_users_pipeline.py -q
```

기대 결과:

```text
41 passed
```

## 2. 20대 여성 1명, 남성 1명 GLM smoke 생성

아래 명령은 로컬 parquet에서 `sex='여자'` 1명과 `sex='남자'` 1명을 읽어 `SourcePersona`로 변환한 뒤 GLM generator를 실행한다.

```powershell
@'
import duckdb

from autoresearch.virtual_users.glm_generator import GLMVirtualUserGenerator
from autoresearch.virtual_users.persona_source import (
    sample_personas_by_contract,
    source_persona_from_record,
    write_raw_persona_records,
)
from autoresearch.virtual_users.pipeline import generate_virtual_user_batch
from autoresearch.virtual_users.schema import GenerationRequest

raw_glob = "Nemotron-Personas-Korea/data/*.parquet"
query = f"""
WITH female AS (
  SELECT * FROM read_parquet('{raw_glob}')
  WHERE age BETWEEN 20 AND 29 AND sex = '여자'
  LIMIT 1
),
male AS (
  SELECT * FROM read_parquet('{raw_glob}')
  WHERE age BETWEEN 20 AND 29 AND sex = '남자'
  LIMIT 1
)
SELECT * FROM female
UNION ALL BY NAME
SELECT * FROM male
"""

raw_rows = duckdb.connect().execute(query).to_arrow_table().to_pylist()
if len(raw_rows) != 2:
    raise RuntimeError(f"2건 smoke 대상 row를 찾지 못했습니다: {len(raw_rows)}")

write_raw_persona_records(raw_rows, "data/raw/personas/nvidia_personas_kr_2rows.jsonl")
records = [source_persona_from_record(row) for row in raw_rows]

request = GenerationRequest(
    male_count=1,
    female_count=1,
    seed=42,
    use_llm=True,
    source_mode="huggingface",
    output_path="asset/virtual_user/glm_two_virtual_users.parquet",
    warehouse_output_path="data/generated/glm_two_virtual_users.jsonl",
)

batch = generate_virtual_user_batch(
    request=request,
    records=records,
    generator=GLMVirtualUserGenerator(),
)

print(batch.summary)
print("parquet=asset/virtual_user/glm_two_virtual_users.parquet")
print("warehouse=data/generated/glm_two_virtual_users.jsonl")
'@ | python -
```

검증 쿼리:

```powershell
@'
import duckdb

path = "asset/virtual_user/glm_two_virtual_users.parquet"
con = duckdb.connect()
print(con.execute(f"""
SELECT
  count(*) AS parquet_rows,
  sum(CASE WHEN source_persona_json IS NULL OR source_persona_json = '' THEN 1 ELSE 0 END) AS missing_source_json_count,
  sum(CASE WHEN category_evidence IS NULL OR category_evidence = '' THEN 1 ELSE 0 END) AS missing_category_evidence_count,
  min(llm_model) AS llm_model
FROM read_parquet('{path}')
""").fetchall())
'@ | python -
```

기대 결과:

```text
parquet_rows=2
missing_source_json_count=0
missing_category_evidence_count=0
llm_model='glm-5.2'
```

## 3. 최대 100건 GLM 생성

아래 명령은 local parquet에서 20대 남녀 후보를 넉넉히 읽고, 남성 50명/여성 50명을 샘플링해 GLM으로 생성한다. API 호출 100번이 발생하므로 비용과 rate limit을 확인한 뒤 실행한다.

```powershell
@'
import duckdb

from autoresearch.virtual_users.glm_generator import GLMVirtualUserGenerator
from autoresearch.virtual_users.persona_source import (
    source_persona_from_record,
    write_raw_persona_records,
)
from autoresearch.virtual_users.pipeline import generate_virtual_user_batch
from autoresearch.virtual_users.schema import GenerationRequest

raw_glob = "Nemotron-Personas-Korea/data/*.parquet"
query = f"""
SELECT *
FROM read_parquet('{raw_glob}')
WHERE age BETWEEN 20 AND 29
  AND sex IN ('남자', '여자')
LIMIT 2000
"""

raw_rows = duckdb.connect().execute(query).to_arrow_table().to_pylist()
records = [source_persona_from_record(row) for row in raw_rows]

request = GenerationRequest(
    male_count=50,
    female_count=50,
    seed=42,
    use_llm=True,
    max_concurrency=1,
    source_mode="huggingface",
    output_path="asset/virtual_user/virtual_users_20s_100.parquet",
    warehouse_output_path="data/generated/virtual_users_kr.jsonl",
)

sampled_records = sample_personas_by_contract(
    records=records,
    age_min=request.age_min,
    age_max=request.age_max,
    male_count=request.male_count,
    female_count=request.female_count,
    seed=request.seed,
)
write_raw_persona_records(
    [record.raw_payload for record in sampled_records],
    "data/raw/personas/nvidia_personas_kr_100.jsonl",
)

batch = generate_virtual_user_batch(
    request=request,
    records=records,
    generator=GLMVirtualUserGenerator(),
)

print(batch.summary)
'@ | python -
```

## 4. 100건 산출물 검증

```powershell
@'
from pathlib import Path
import duckdb

parquet_path = "asset/virtual_user/virtual_users_20s_100.parquet"
warehouse_path = Path("data/generated/virtual_users_kr.jsonl")
raw_snapshot_path = Path("data/raw/personas/nvidia_personas_kr_100.jsonl")

allowed = [
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

def sql_value(text: str) -> str:
    escaped = text.replace("'", "''")
    return f"('{escaped}')"

values_sql = ", ".join(sql_value(category) for category in allowed)

con = duckdb.connect()
summary = con.execute(f"""
SELECT
  count(*) AS parquet_rows,
  sum(CASE WHEN source_persona_json IS NULL OR source_persona_json = '' THEN 1 ELSE 0 END) AS missing_source_json_count,
  sum(CASE WHEN category_evidence IS NULL OR category_evidence = '' THEN 1 ELSE 0 END) AS missing_category_evidence_count,
  count(DISTINCT source_uuid) AS distinct_source_uuid_count
FROM read_parquet('{parquet_path}')
""").fetchone()

invalid_category_count = con.execute(f"""
WITH allowed(category) AS (VALUES {values_sql}),
flat AS (
  SELECT category
  FROM read_parquet('{parquet_path}'),
  UNNEST(primary_categories) AS t(category)
)
SELECT count(*)
FROM flat
LEFT JOIN allowed USING (category)
WHERE allowed.category IS NULL
""").fetchone()[0]

warehouse_jsonl_lines = len(warehouse_path.read_text(encoding="utf-8").splitlines())
raw_snapshot_rows = len(raw_snapshot_path.read_text(encoding="utf-8").splitlines())

print(f"parquet_rows={summary[0]}")
print(f"raw_snapshot_rows={raw_snapshot_rows}")
print(f"missing_source_json_count={summary[1]}")
print(f"missing_category_evidence_count={summary[2]}")
print(f"distinct_source_uuid_count={summary[3]}")
print(f"invalid_category_count={invalid_category_count}")
print(f"warehouse_jsonl_lines={warehouse_jsonl_lines}")
'@ | python -
```

기대 결과:

```text
parquet_rows=100
raw_snapshot_rows=100
missing_source_json_count=0
missing_category_evidence_count=0
distinct_source_uuid_count=100
invalid_category_count=0
warehouse_jsonl_lines=100
```

## 확인 포인트

- GLM 응답은 `DerivedVirtualUserFeatures`만 통과한다.
- `age`, `sex`, `occupation`, `province`, `district`, `country`, `locale`, `source_uuid`는 GLM 응답이 아니라 `SourcePersona`에서 복사된다.
- `category_affinity`는 GLM 숫자가 아니라 `primary_categories` 순위와 `category_evidence` 개수로 deterministic하게 계산된다.
- parquet의 `category_evidence`, `source_persona_json`은 DuckDB 검증 편의를 위해 JSON string으로 저장된다.
- warehouse JSONL의 `category_evidence`, `source_persona_json`은 dict 형태로 저장된다.
