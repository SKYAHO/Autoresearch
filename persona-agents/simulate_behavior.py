"""Cloud Run Job: 페르소나(가상 유저)가 유튜브 영상 목록을 보고 행동하도록 시뮬레이션.

각 가상 유저에게 후보 영상 슬레이트를 제시하고, 영상마다
  - clicked      클릭 여부
  - watch_ratio  시청 지속 비율(0~1, 클릭했을 때만 의미)
  - liked        좋아요 여부
를 결정하게 한 뒤, 상호작용(interaction) 이벤트로 BigQuery + GCS 에 저장한다.
→ 이후 추천 모델(BQML / Vertex Two-Tower) 학습 데이터로 사용.

환경변수
  GCP_PROJECT      (필수)
  PERSONAS_TABLE   (필수) project.dataset.personas
  VIDEOS_TABLE     (필수) 후보 영상 카탈로그 (예: <proj>.youtube.trending)
  OUTPUT_TABLE     (필수) project.dataset.interactions
  GCS_BUCKET       (필수) JSONL 백업
  SAMPLE_USERS     가상 유저 수 (기본 20)
  POOL_SIZE        후보 영상 풀 크기 (기본 80)
  SLATE_SIZE       유저당 노출 영상 수 (기본 15)
  BQ_LOCATION      기본 asia-northeast3
  GEMINI_MODEL / VERTEX_LOCATION  (common.py 참고)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import random
import tempfile
import uuid
from pathlib import Path

from google.cloud import bigquery, storage

import common


def env_int(name: str, default: int) -> int:
    try:
        value = int(os.getenv(name, str(default)))
    except ValueError as exc:
        raise SystemExit(f"오류: {name} 환경변수는 정수여야 합니다.") from exc
    if value < 1:
        raise SystemExit(f"오류: {name} 환경변수는 1 이상이어야 합니다.")
    return value


def coerce_bool(value) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"true", "1", "yes", "y"}
    return bool(value)


PERSONAS_TABLE = os.environ["PERSONAS_TABLE"]
VIDEOS_TABLE = os.environ["VIDEOS_TABLE"]
OUTPUT_TABLE = os.environ["OUTPUT_TABLE"]
GCS_BUCKET = os.environ["GCS_BUCKET"]
SAMPLE_USERS = env_int("SAMPLE_USERS", 20)
POOL_SIZE = env_int("POOL_SIZE", 80)
SLATE_SIZE = env_int("SLATE_SIZE", 15)
BQ_LOCATION = os.getenv("BQ_LOCATION", "asia-northeast3")

OUTPUT_SCHEMA = [
    bigquery.SchemaField("run_id", "STRING"),
    bigquery.SchemaField("persona_id", "STRING"),
    bigquery.SchemaField("video_id", "STRING"),
    bigquery.SchemaField("rank", "INT64"),
    bigquery.SchemaField("clicked", "BOOL"),
    bigquery.SchemaField("watch_ratio", "FLOAT64"),
    bigquery.SchemaField("liked", "BOOL"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
]


def fetch_video_pool(bq: bigquery.Client) -> list[dict]:
    sql = f"""
        WITH latest_day AS (
            SELECT MAX(video_trending__date) AS d
            FROM `{VIDEOS_TABLE}`
            WHERE video_id IS NOT NULL
        )
        SELECT video_id, video_title, video_category_id, channel_title, video_view_count
        FROM `{VIDEOS_TABLE}`, latest_day
        WHERE video_id IS NOT NULL
          AND video_trending__date = latest_day.d
        ORDER BY RAND()
        LIMIT {POOL_SIZE}
    """
    return [dict(r) for r in bq.query(sql).result()]


def sample_personas(bq: bigquery.Client) -> list[dict]:
    sql = f"SELECT * FROM `{PERSONAS_TABLE}` WHERE RAND() < 0.05 LIMIT {SAMPLE_USERS}"
    rows = [dict(r) for r in bq.query(sql).result()]
    if len(rows) >= SAMPLE_USERS:
        return rows
    fallback = f"SELECT * FROM `{PERSONAS_TABLE}` ORDER BY RAND() LIMIT {SAMPLE_USERS}"
    return [dict(r) for r in bq.query(fallback).result()]


def slate_text(slate: list[dict]) -> str:
    lines = []
    for i, v in enumerate(slate, 1):
        lines.append(
            f"{i}. {v.get('video_title')} "
            f"| 카테고리:{v.get('video_category_id')} "
            f"| 채널:{v.get('channel_title')} "
            f"| 조회수:{v.get('video_view_count')}"
        )
    return "\n".join(lines)


def simulate_user(system_prompt: str, slate: list[dict]) -> list[dict]:
    """슬레이트에 대한 행동 결정 리스트 반환: [{no, clicked, watch_ratio, liked}]."""
    prompt = (
        "아래는 지금 당신에게 노출된 유튜브 추천 목록입니다.\n"
        "실제로 이 목록을 훑어볼 때 각 영상에 대해 당신(인물)의 취향에 따라 결정하세요.\n"
        "- clicked: 클릭하겠는가 (true/false)\n"
        "- watch_ratio: 클릭했다면 얼마나 볼지 0~1 (클릭 안 하면 0)\n"
        "- liked: 좋아요를 누를지 (true/false)\n\n"
        f"{slate_text(slate)}\n\n"
        "반드시 모든 항목에 대해 JSON 배열로만 답하세요. 형식:\n"
        '[{"no":1,"clicked":true,"watch_ratio":0.8,"liked":false}, ...]'
    )
    raw = common.gemini_generate(
        system_prompt, prompt, temperature=0.8, response_mime_type="application/json"
    )
    return common.parse_json_array(raw)


def ensure_table(bq: bigquery.Client) -> None:
    from google.api_core.exceptions import NotFound

    try:
        bq.get_table(OUTPUT_TABLE)
    except NotFound:
        t = bigquery.Table(OUTPUT_TABLE, schema=OUTPUT_SCHEMA)
        t.time_partitioning = bigquery.TimePartitioning(field="created_at")
        bq.create_table(t)
        print(f"출력 테이블 생성: {OUTPUT_TABLE}")


def main() -> None:
    run_id = dt.datetime.now(dt.timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    bq = bigquery.Client(location=BQ_LOCATION)
    ensure_table(bq)

    pool = fetch_video_pool(bq)
    personas = sample_personas(bq)
    if len(pool) < 1 or not personas:
        raise SystemExit("영상 풀 또는 페르소나가 비어 있습니다. 적재 상태를 확인하세요.")
    print(f"run {run_id}: 유저 {len(personas)} × 슬레이트 {min(SLATE_SIZE, len(pool))} (풀 {len(pool)})")

    rows: list[dict] = []
    for p in personas:
        system_prompt = common.build_system_prompt(p)
        pid = common.persona_id(p, fallback=str(uuid.uuid4()))
        slate = random.sample(pool, k=min(SLATE_SIZE, len(pool)))
        try:
            decisions = simulate_user(system_prompt, slate)
        except Exception as exc:  # noqa: BLE001
            print(f"  시뮬레이션 실패(persona={pid}): {exc}")
            continue
        by_no = {}
        for d in decisions:
            if not isinstance(d, dict):
                continue
            try:
                no = int(d.get("no", 0))
            except (TypeError, ValueError):
                continue
            if no > 0:
                by_no[no] = d
        for i, v in enumerate(slate, 1):
            d = by_no.get(i, {})
            clicked = coerce_bool(d.get("clicked", False))
            wr = d.get("watch_ratio", 0) or 0
            try:
                wr = max(0.0, min(1.0, float(wr)))
            except (TypeError, ValueError):
                wr = 0.0
            liked = clicked and coerce_bool(d.get("liked", False))
            rows.append({
                "run_id": run_id,
                "persona_id": pid,
                "video_id": v.get("video_id"),
                "rank": i,
                "clicked": clicked,
                "watch_ratio": wr if clicked else 0.0,
                "liked": liked,
                "created_at": now,
            })
    print(f"생성된 상호작용 이벤트: {len(rows)}")
    if not rows:
        return

    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / f"interactions_{run_id}.jsonl"
        local.write_text(
            "\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8"
        )
        blob = f"persona_interactions/{run_id}.jsonl"
        storage.Client().bucket(GCS_BUCKET).blob(blob).upload_from_filename(str(local))
        print(f"GCS 저장: gs://{GCS_BUCKET}/{blob}")

    job = bq.load_table_from_json(
        rows, OUTPUT_TABLE,
        job_config=bigquery.LoadJobConfig(
            schema=OUTPUT_SCHEMA, write_disposition="WRITE_APPEND"
        ),
    )
    job.result()
    print(f"BigQuery append 완료: {OUTPUT_TABLE} (+{len(rows)} rows)")


if __name__ == "__main__":
    main()
