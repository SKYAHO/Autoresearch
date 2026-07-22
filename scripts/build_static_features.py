"""정적(persona 기반) Feast feature 테이블을 BigQuery에 적재하는 1회성 스크립트.

동적 feature(user_dynamic_feature, video_feature)와 달리 아래 두 테이블은
persona/카테고리 설명문이 바뀔 때만 갱신되는 정적 feature다. 매일 도는
feast_offline_feature_build DAG와 분리해, persona 재생성 시점에만 수동으로
실행한다.

적재 대상
  1. feast_offline_store.user_static_feature
     - GCS persona parquet을 external table로 직접 읽어 재구축한다
       (BQ asset_virtual_user_vu_1000 테이블에 의존하지 않는다 — 삭제 예정).
  2. autoresearch_dev_analytics.user_topic_embedding   (중간 산출물)
     - persona 관심 키워드를 Vertex AI(text-multilingual-embedding-002,
       RETRIEVAL_QUERY)로 임베딩한 (user_id, topic, vector) 행별 테이블.
  3. autoresearch_dev_analytics.category_embedding      (중간 산출물)
     - src/features/category_reference.py의 15개 카테고리 설명문을
       RETRIEVAL_DOCUMENT로 임베딩한 참조 테이블.
  4. feast_offline_store.user_category_similarity
     - 위 두 임베딩 테이블의 cosine 유사도. 전체 유저 × 15 카테고리 grid에
       LEFT JOIN해, 키워드 없는 유저도 topic_similarity=0.0으로 누락 없이
       포함한다.

계층 분리
  - 임베딩 중간 산출물은 Feast feature table이 아니므로 feast_offline_store가
    아니라 analytics dataset에 만든다.
  - terraform이 스키마를 소유하는 user_static_feature / user_category_similarity는
    TRUNCATE + INSERT로 적재해 REQUIRED/REPEATED mode를 보존한다. terraform이
    소유하지 않는 임베딩 테이블은 CREATE OR REPLACE로 만든다.

SQL 계약 출처: docs/guides/data-warehouse.md.

사용법
  python scripts/build_static_features.py                 # 4단계 전부
  python scripts/build_static_features.py --steps user_static_feature
  python scripts/build_static_features.py --dry-run       # BQ dry-run(임베딩은 스킵)
"""

from __future__ import annotations

import argparse
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover - 실행 환경 안내
    print("python-dotenv 가 필요합니다: uv sync")
    sys.exit(1)

# src.features.embeddings의 단일 출처 상수를 그대로 쓴다.
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from src.features.embeddings import EMBEDDING_DIM, EMBEDDING_MODEL  # noqa: E402

DEFAULT_PROJECT = "ar-infra-501607"
DEFAULT_FEATURE_DATASET = "feast_offline_store"
DEFAULT_EMBEDDING_DATASET = "autoresearch_dev_analytics"
DEFAULT_LOCATION = "asia-northeast3"
DEFAULT_PERSONA_PATH = "asset/virtual_user/vu_1000.parquet"

# 정적 feature의 valid-from timestamp. static persona feature가 모든 action log
# 이전부터 유효하다는 규약(docs/guides/data-warehouse.md)에 따라 epoch로 고정한다.
EPOCH_TS = "1970-01-01 00:00:00 UTC"

USER_TOPIC_EMBEDDING_VERSION = "user_topic_embedding_v1"
CATEGORY_EMBEDDING_VERSION = "category_embedding_v1"

# Vertex AI 분당 쿼터 대응. embed_texts()는 250건 청크를 간격 없이 연속
# 호출하므로 46k건 규모에서는 ResourceExhausted(429)가 난다. 슬라이스마다
# 끊어 호출하고 사이에 쉰다.
EMBED_SLICE_SIZE = 250
EMBED_SLICE_PAUSE_SEC = 2.0
EMBED_QUOTA_RETRIES = 6
EMBED_QUOTA_BACKOFF_SEC = 30

# persona parquet에서 preferred_topics를 구성하는 키워드 array 컬럼들.
# docs/guides/data-warehouse.md user_static_feature 규칙과 동일한 순서.
KEYWORD_SOURCE_COLUMNS = (
    "hobby_keywords",
    "interest_keywords",
    "lifestyle_keywords",
    "food_keywords",
    "travel_keywords",
    "career_keywords",
    "family_context_keywords",
)

STEPS = (
    "user_static_feature",
    "user_topic_embedding",
    "category_embedding",
    "user_category_similarity",
)


@dataclass(frozen=True)
class Settings:
    project: str
    feature_dataset: str
    embedding_dataset: str
    location: str
    bucket: str
    persona_path: str
    dry_run: bool
    cache_dir: Path | None


# ---------------------------------------------------------------------------
# SQL 빌더 (순수 함수 — 네트워크 없음, 테스트 대상)
# ---------------------------------------------------------------------------


def user_static_feature_sql(settings: Settings) -> str:
    """GCS persona external table을 읽어 user_static_feature를 재구축하는 DML.

    TRUNCATE + INSERT라 terraform 소유 스키마(REQUIRED/REPEATED)를 보존한다.
    ``persona`` alias는 실행 시점에 external table definition으로 바인딩한다.
    """

    target = f"`{settings.project}.{settings.feature_dataset}.user_static_feature`"
    return f"""\
TRUNCATE TABLE {target};
INSERT INTO {target} (
  user_id, event_timestamp, age_group, occupation,
  preferred_category, preferred_topics, watch_time_band
)
SELECT
  user_id,
  TIMESTAMP '{EPOCH_TS}' AS event_timestamp,
  COALESCE(age_bucket, 'unknown') AS age_group,
  COALESCE(occupation, 'unknown') AS occupation,
  COALESCE(primary_categories, ARRAY<STRING>[]) AS preferred_category,
  ARRAY_CONCAT(
    COALESCE(hobby_keywords, ARRAY<STRING>[]),
    COALESCE(interest_keywords, ARRAY<STRING>[]),
    COALESCE(lifestyle_keywords, ARRAY<STRING>[]),
    COALESCE(food_keywords, ARRAY<STRING>[]),
    COALESCE(travel_keywords, ARRAY<STRING>[]),
    COALESCE(career_keywords, ARRAY<STRING>[]),
    COALESCE(family_context_keywords, ARRAY<STRING>[])
  ) AS preferred_topics,
  CASE
    WHEN LOWER(TRIM(watch_time_band)) IN ('morning', 'am', '오전', '아침') THEN 'morning'
    WHEN LOWER(TRIM(watch_time_band)) IN ('evening', 'pm', '저녁', '오후') THEN 'evening'
    WHEN LOWER(TRIM(watch_time_band)) IN ('night', 'late_night', '밤', '심야') THEN 'night'
    ELSE 'unknown'
  END AS watch_time_band
FROM persona
WHERE user_id IS NOT NULL;
"""


def user_category_similarity_sql(settings: Settings) -> str:
    """전체 유저 × 15 카테고리 grid에 cosine 유사도를 LEFT JOIN하는 DML.

    grid를 user_static_feature(전체 유저)와 category_embedding(전체 카테고리)로
    만들고, topic이 있는 (user, category)만 cosine을 계산해 LEFT JOIN한다.
    그래서 키워드가 없는 유저도 topic_similarity=0.0, top_topic='unknown'으로
    반드시 15개 row가 생긴다(docs/guides/data-warehouse.md 규칙 6, 누락 방지).

    event_timestamp/embedding_model/embedding_dim은 원본 테이블 값이 아니라
    이 스크립트 상수로 채워, 정적 feature 시맨틱과 모델 일관성을 보장한다.

    cosine은 ML.DISTANCE로 계산한다. docs/guides/data-warehouse.md의 SQL은 768
    차원을 UNNEST로 행으로 펼쳐 내적하는데, 그러면 중간 결과가
    140,934 topic × 15 카테고리 × 768 = 약 16억 행까지 부풀어 실측에서 33분
    동안 출력 stage가 0행에 머물렀다(평균 8.8 slot, 사실상 병렬화 실패).
    ML.DISTANCE는 배열을 그대로 받아 같은 값을 계산한다.
    """

    feat = f"{settings.project}.{settings.feature_dataset}"
    emb = f"{settings.project}.{settings.embedding_dataset}"
    target = f"`{feat}.user_category_similarity`"
    return f"""\
DECLARE ute_version STRING DEFAULT '{USER_TOPIC_EMBEDDING_VERSION}';
DECLARE ce_version STRING DEFAULT '{CATEGORY_EMBEDDING_VERSION}';

TRUNCATE TABLE {target};

INSERT INTO {target} (
  user_id, category_id, event_timestamp,
  topic_similarity, topic_similarity_top_topic,
  embedding_model, embedding_dim,
  user_topic_embedding_version, category_embedding_version,
  similarity_method, similarity_pooling
)
WITH categories AS (
  SELECT category_id, category_embedding, embedding_model, embedding_dim
  FROM `{emb}.category_embedding`
  WHERE category_id IS NOT NULL
    AND category_embedding IS NOT NULL
    AND embedding_version = ce_version
),
users AS (
  SELECT DISTINCT user_id
  FROM `{feat}.user_static_feature`
  WHERE user_id IS NOT NULL
),
grid AS (
  SELECT u.user_id, c.category_id
  FROM users u
  CROSS JOIN categories c
),
user_topics AS (
  SELECT user_id, topic, topic_embedding, embedding_model, embedding_dim
  FROM `{emb}.user_topic_embedding`
  WHERE user_id IS NOT NULL
    AND topic IS NOT NULL
    AND topic_embedding IS NOT NULL
    AND embedding_version = ute_version
),
best AS (
  SELECT
    u.user_id,
    c.category_id,
    u.topic,
    1 - ML.DISTANCE(u.topic_embedding, c.category_embedding, 'COSINE') AS cosine_score
  FROM user_topics u
  JOIN categories c
    ON u.embedding_model = c.embedding_model
   AND u.embedding_dim = c.embedding_dim
  QUALIFY ROW_NUMBER() OVER (
    PARTITION BY u.user_id, c.category_id
    ORDER BY cosine_score DESC, u.topic
  ) = 1
)
SELECT
  g.user_id,
  g.category_id,
  TIMESTAMP '{EPOCH_TS}' AS event_timestamp,
  COALESCE(b.cosine_score, 0.0) AS topic_similarity,
  COALESCE(b.topic, 'unknown') AS topic_similarity_top_topic,
  '{EMBEDDING_MODEL}' AS embedding_model,
  {EMBEDDING_DIM} AS embedding_dim,
  ute_version AS user_topic_embedding_version,
  ce_version AS category_embedding_version,
  'cosine' AS similarity_method,
  'max' AS similarity_pooling
FROM grid g
LEFT JOIN best b
  USING (user_id, category_id);
"""


# ---------------------------------------------------------------------------
# persona 키워드 추출 (순수 함수 — 테스트 대상)
# ---------------------------------------------------------------------------


def extract_topic_rows(persona_df) -> list[dict]:
    """persona DataFrame에서 (user_id, topic, topic_source) 행을 만든다.

    7개 키워드 array 컬럼을 explode하고, 같은 유저의 (topic, source) 중복은
    제거한다. topic_source는 원본 키워드 컬럼명을 보존한다.
    """

    rows: list[dict] = []
    for record in persona_df.itertuples(index=False):
        user_id = getattr(record, "user_id", None)
        if not user_id:
            continue
        seen: set[tuple[str, str]] = set()
        for source in KEYWORD_SOURCE_COLUMNS:
            keywords = getattr(record, source, None)
            if keywords is None:
                continue
            for keyword in keywords:
                if not keyword or not isinstance(keyword, str):
                    continue
                topic = keyword.strip()
                if not topic or (topic, source) in seen:
                    continue
                seen.add((topic, source))
                rows.append(
                    {"user_id": user_id, "topic": topic, "topic_source": source}
                )
    return rows


def unique_topics(topic_rows: list[dict]) -> list[str]:
    """임베딩 API 호출을 줄이기 위해 고유 topic 문자열만 순서대로 모은다."""

    ordered: list[str] = []
    seen: set[str] = set()
    for row in topic_rows:
        topic = row["topic"]
        if topic not in seen:
            seen.add(topic)
            ordered.append(topic)
    return ordered


# ---------------------------------------------------------------------------
# 실행 (BigQuery / Vertex AI)
# ---------------------------------------------------------------------------


def _external_persona_config(settings: Settings):
    from google.cloud import bigquery

    ext = bigquery.ExternalConfig("PARQUET")
    ext.source_uris = [f"gs://{settings.bucket}/{settings.persona_path}"]
    # parquet의 3-level LIST 인코딩을 native ARRAY<STRING>으로 해석하게 한다.
    # 이 옵션이 없으면 키워드 컬럼이 STRUCT<list ...>로 노출돼 ARRAY 연산이
    # 전부 깨진다.
    parquet_options = bigquery.format_options.ParquetOptions()
    parquet_options.enable_list_inference = True
    ext.parquet_options = parquet_options
    return ext


def _run_script(client, sql: str, settings: Settings, *, external_persona: bool):
    from google.cloud import bigquery

    job_config = bigquery.QueryJobConfig(dry_run=settings.dry_run)
    if external_persona:
        job_config.table_definitions = {"persona": _external_persona_config(settings)}
    job = client.query(sql, job_config=job_config, location=settings.location)
    if not settings.dry_run:
        job.result()
    return job


def build_user_static_feature(client, settings: Settings) -> None:
    print("[user_static_feature] GCS persona external table -> TRUNCATE+INSERT")
    _run_script(
        client,
        user_static_feature_sql(settings),
        settings,
        external_persona=True,
    )
    print("  [OK]")


def _load_embedding_table(client, table_id: str, rows: list[dict], schema, settings):
    """임베딩 행을 Parquet으로 직렬화해 BigQuery에 적재한다.

    load_table_from_json은 쓰지 않는다 — user_topic_embedding은 140,934행 ×
    768 float(약 1억 800만 개)이라 JSON 텍스트 직렬화가 사실상 끝나지 않는다
    (실측: 9분 경과 시점에도 load job 제출 전). Parquet은 float 배열을 바이너리로
    쓰므로 같은 데이터를 수십 배 빠르게 적재한다.
    """

    import pandas as pd
    from google.cloud import bigquery

    if settings.dry_run:
        print(f"  [dry-run] {table_id} <- {len(rows)} rows (skip load)")
        return
    job_config = bigquery.LoadJobConfig(
        schema=schema,
        write_disposition=bigquery.WriteDisposition.WRITE_TRUNCATE,
        source_format=bigquery.SourceFormat.PARQUET,
    )
    frame = pd.DataFrame(rows)
    # Parquet은 JSON과 달리 타입이 엄격하다. TIMESTAMP 컬럼은 문자열이 아니라
    # tz-aware datetime이어야 pyarrow가 변환할 수 있다.
    if "event_timestamp" in frame.columns:
        frame["event_timestamp"] = pd.to_datetime(
            frame["event_timestamp"], utc=True
        )
    job = client.load_table_from_dataframe(
        frame, table_id, job_config=job_config, location=settings.location
    )
    job.result()
    print(f"  [OK] {table_id} <- {client.get_table(table_id).num_rows} rows")


def _cache_path(settings: Settings, name: str) -> Path | None:
    if settings.cache_dir is None:
        return None
    return settings.cache_dir / f"{name}_embedding_cache.jsonl"


def _load_embedding_cache(path: Path | None) -> dict[str, list[float]]:
    """이전 실행이 남긴 임베딩 캐시를 읽는다.

    46k건 규모라 중간에 쿼터로 끊기면 재실행 비용이 크다. 캐시가 있으면 이미
    임베딩한 topic은 API를 다시 호출하지 않는다.

    형식은 JSON Lines다 — 슬라이스마다 append만 하면 되므로, 전량을 다시 쓰는
    방식(784MB × 185회 ≈ 72GB 쓰기)의 O(n^2) 비용을 피한다. 같은 topic이 여러 번
    있으면 나중 값이 이긴다. 손상된 마지막 줄은 무시한다(쓰다가 죽은 경우).
    """

    if path is None or not path.exists():
        return {}
    cached: dict[str, list[float]] = {}
    with path.open(encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            cached[record["topic"]] = record["vector"]
    print(f"  캐시 {len(cached)}건을 재사용합니다: {path}")
    return cached


def _append_embedding_cache(
    path: Path | None, pairs: list[tuple[str, list[float]]]
) -> None:
    """새로 임베딩한 (topic, vector)만 캐시 파일에 append한다."""

    if path is None or not pairs:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as handle:
        for topic, vector in pairs:
            handle.write(
                json.dumps({"topic": topic, "vector": vector}, ensure_ascii=False)
            )
            handle.write("\n")


def _embed(
    texts: list[str],
    task_type: str,
    settings: Settings,
    cache_path: Path | None = None,
) -> list[list[float]]:
    """Vertex AI 임베딩을 쿼터에 맞춰 나눠 호출한다.

    embed_texts()는 청크를 간격 없이 연속 호출하므로 46k건 규모에서는 Vertex AI
    분당 쿼터를 넘겨 ResourceExhausted(429)로 죽는다. 여기서 슬라이스 단위로
    끊어 호출하고 사이에 쉬며, 쿼터 오류는 긴 백오프로 재시도한다. 성공한
    슬라이스는 즉시 캐시에 기록해 중간에 실패해도 재실행이 이어지게 한다.
    """

    if settings.dry_run:
        # dry-run은 임베딩 API를 호출하지 않는다. 0벡터 placeholder.
        return [[0.0] * EMBEDDING_DIM for _ in texts]

    import time

    from google.api_core.exceptions import ResourceExhausted, ServiceUnavailable
    from tenacity import RetryError

    from src.features.embeddings import embed_texts

    cache = _load_embedding_cache(cache_path)
    pending = [text for text in texts if text not in cache]
    print(f"  임베딩 대상 {len(pending)}건 (캐시 적중 {len(texts) - len(pending)}건)")

    quota_errors = (ResourceExhausted, ServiceUnavailable, RetryError)
    for start in range(0, len(pending), EMBED_SLICE_SIZE):
        chunk = pending[start : start + EMBED_SLICE_SIZE]
        for attempt in range(1, EMBED_QUOTA_RETRIES + 1):
            try:
                vectors = embed_texts(chunk, task_type=task_type)
                break
            except quota_errors as exc:
                if attempt == EMBED_QUOTA_RETRIES:
                    raise RuntimeError(
                        f"임베딩 쿼터 재시도 {EMBED_QUOTA_RETRIES}회 실패 "
                        f"({type(exc).__name__}). 캐시를 저장했으니 재실행하면 "
                        f"{len(cache)}건을 건너뜁니다."
                    ) from exc
                backoff = EMBED_QUOTA_BACKOFF_SEC * attempt
                print(f"  쿼터 대기 {backoff}s (시도 {attempt}/{EMBED_QUOTA_RETRIES})")
                time.sleep(backoff)
        fresh = [(text, vector.tolist()) for text, vector in zip(chunk, vectors)]
        cache.update(dict(fresh))
        _append_embedding_cache(cache_path, fresh)
        done = min(start + EMBED_SLICE_SIZE, len(pending))
        print(f"  진행 {done}/{len(pending)}", flush=True)
        if done < len(pending):
            time.sleep(EMBED_SLICE_PAUSE_SEC)

    return [cache[text] for text in texts]


def build_user_topic_embedding(client, settings: Settings) -> None:
    import pandas as pd
    from google.cloud import bigquery

    print("[user_topic_embedding] persona 키워드 임베딩")
    uri = f"gs://{settings.bucket}/{settings.persona_path}"
    persona_df = pd.read_parquet(uri, columns=["user_id", *KEYWORD_SOURCE_COLUMNS])
    topic_rows = extract_topic_rows(persona_df)
    topics = unique_topics(topic_rows)
    print(f"  users={persona_df['user_id'].nunique()} topic_rows={len(topic_rows)} unique_topics={len(topics)}")

    vectors = _embed(
        topics, "RETRIEVAL_QUERY", settings, _cache_path(settings, "user_topic")
    )
    topic_to_vec = dict(zip(topics, vectors))

    rows = [
        {
            "user_id": r["user_id"],
            "event_timestamp": EPOCH_TS.replace(" UTC", "+00:00"),
            "topic": r["topic"],
            "topic_embedding": topic_to_vec[r["topic"]],
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dim": EMBEDDING_DIM,
            "embedding_version": USER_TOPIC_EMBEDDING_VERSION,
            "topic_source": r["topic_source"],
        }
        for r in topic_rows
    ]
    schema = [
        bigquery.SchemaField("user_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("event_timestamp", "TIMESTAMP", mode="REQUIRED"),
        bigquery.SchemaField("topic", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("topic_embedding", "FLOAT64", mode="REPEATED"),
        bigquery.SchemaField("embedding_model", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("embedding_dim", "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("embedding_version", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("topic_source", "STRING", mode="NULLABLE"),
    ]
    table_id = (
        f"{settings.project}.{settings.embedding_dataset}.user_topic_embedding"
    )
    _load_embedding_table(client, table_id, rows, schema, settings)


def build_category_embedding(client, settings: Settings) -> None:
    from google.cloud import bigquery

    from src.features.category_reference import CATEGORY_DESCRIPTIONS

    print("[category_embedding] 15개 카테고리 설명문 임베딩")
    names = list(CATEGORY_DESCRIPTIONS.keys())
    descriptions = list(CATEGORY_DESCRIPTIONS.values())
    vectors = _embed(
        descriptions,
        "RETRIEVAL_DOCUMENT",
        settings,
        _cache_path(settings, "category"),
    )
    rows = [
        {
            "category_id": name,
            "category_name": name,
            "category_description": desc,
            "category_embedding": vec,
            "embedding_model": EMBEDDING_MODEL,
            "embedding_dim": EMBEDDING_DIM,
            "embedding_version": CATEGORY_EMBEDDING_VERSION,
        }
        for name, desc, vec in zip(names, descriptions, vectors)
    ]
    schema = [
        bigquery.SchemaField("category_id", "STRING", mode="REQUIRED"),
        bigquery.SchemaField("category_name", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("category_description", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("category_embedding", "FLOAT64", mode="REPEATED"),
        bigquery.SchemaField("embedding_model", "STRING", mode="NULLABLE"),
        bigquery.SchemaField("embedding_dim", "INTEGER", mode="NULLABLE"),
        bigquery.SchemaField("embedding_version", "STRING", mode="NULLABLE"),
    ]
    table_id = f"{settings.project}.{settings.embedding_dataset}.category_embedding"
    _load_embedding_table(client, table_id, rows, schema, settings)


def build_user_category_similarity(client, settings: Settings) -> None:
    print("[user_category_similarity] cosine grid -> TRUNCATE+INSERT")
    _run_script(
        client,
        user_category_similarity_sql(settings),
        settings,
        external_persona=False,
    )
    print("  [OK]")


STEP_FUNCS = {
    "user_static_feature": build_user_static_feature,
    "user_topic_embedding": build_user_topic_embedding,
    "category_embedding": build_category_embedding,
    "user_category_similarity": build_user_category_similarity,
}


def select_steps(steps_arg: str | None) -> list[str]:
    if not steps_arg:
        return list(STEPS)
    requested = [s.strip() for s in steps_arg.split(",") if s.strip()]
    unknown = [s for s in requested if s not in STEP_FUNCS]
    if unknown:
        raise ValueError(
            f"알 수 없는 step: {', '.join(unknown)} (사용 가능: {', '.join(STEPS)})"
        )
    # 의존 순서를 유지하기 위해 STEPS 순서로 정렬한다.
    return [s for s in STEPS if s in requested]


def main(argv: list[str] | None = None) -> int:
    load_dotenv()
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", default=os.getenv("GCP_PROJECT_ID", DEFAULT_PROJECT))
    parser.add_argument(
        "--feature-dataset",
        default=os.getenv("BQ_DATASET", DEFAULT_FEATURE_DATASET),
        help="terraform 소유 Feast feature 테이블 dataset",
    )
    parser.add_argument(
        "--embedding-dataset",
        default=os.getenv("BQ_EMBEDDING_DATASET", DEFAULT_EMBEDDING_DATASET),
        help="임베딩 중간 산출물 dataset (Feast 아님)",
    )
    parser.add_argument("--location", default=os.getenv("BQ_LOCATION", DEFAULT_LOCATION))
    parser.add_argument("--bucket", default=os.getenv("YOUTUBE_LAKE_BUCKET"))
    parser.add_argument("--persona-path", default=DEFAULT_PERSONA_PATH)
    parser.add_argument("--steps", default=None, help="실행할 step 쉼표 구분 (기본 전부)")
    parser.add_argument(
        "--cache-dir",
        default=".embedding_cache",
        help="임베딩 캐시 디렉터리. 빈 문자열이면 캐시를 쓰지 않는다",
    )
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    if not args.bucket:
        parser.error("--bucket 또는 YOUTUBE_LAKE_BUCKET 이 필요합니다")
    if args.feature_dataset == args.embedding_dataset:
        parser.error("--feature-dataset 과 --embedding-dataset 은 달라야 합니다")

    try:
        steps = select_steps(args.steps)
    except ValueError as exc:
        parser.error(str(exc))

    settings = Settings(
        project=args.project,
        feature_dataset=args.feature_dataset,
        embedding_dataset=args.embedding_dataset,
        location=args.location,
        bucket=args.bucket,
        persona_path=args.persona_path,
        dry_run=args.dry_run,
        cache_dir=Path(args.cache_dir) if args.cache_dir else None,
    )

    from google.cloud import bigquery

    client = bigquery.Client(project=settings.project, location=settings.location)
    print(f"project={settings.project} feature_ds={settings.feature_dataset} "
          f"embedding_ds={settings.embedding_dataset} dry_run={settings.dry_run}")
    for step in steps:
        STEP_FUNCS[step](client, settings)
    print("done.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
