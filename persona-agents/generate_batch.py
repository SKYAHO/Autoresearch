"""Cloud Run Job: 페르소나 × 시나리오 합성 대화 데이터 대량 생성 → BigQuery + GCS.

BigQuery personas 테이블에서 N명을 샘플링하고, 각 페르소나가 주어진 시나리오에서
실제 사용자처럼 대화하도록 Gemini로 멀티턴 대화를 생성한다.

환경변수
  GCP_PROJECT          (필수)
  PERSONAS_TABLE       (필수) project.dataset.personas
  OUTPUT_TABLE         (필수) project.dataset.persona_dialogues
  GCS_BUCKET           (필수) JSONL 백업 저장
  SAMPLE_SIZE          샘플 페르소나 수 (기본 20)
  TURNS                대화 턴 수 (기본 4)
  SCENARIOS            시나리오 목록(줄바꿈 구분). 미지정 시 기본값 사용
  BQ_LOCATION          기본 asia-northeast3
  GEMINI_MODEL / VERTEX_LOCATION  (common.py 참고)
"""

from __future__ import annotations

import datetime as dt
import json
import os
import tempfile
import uuid
from pathlib import Path

from google.cloud import bigquery, storage

import common

PERSONAS_TABLE = os.environ["PERSONAS_TABLE"]
OUTPUT_TABLE = os.environ["OUTPUT_TABLE"]
GCS_BUCKET = os.environ["GCS_BUCKET"]
SAMPLE_SIZE = int(os.getenv("SAMPLE_SIZE", "20"))
TURNS = int(os.getenv("TURNS", "4"))
BQ_LOCATION = os.getenv("BQ_LOCATION", "asia-northeast3")

DEFAULT_SCENARIOS = [
    "새로 나온 스마트폰을 구매할지 고민하며 친구에게 의견을 묻는다.",
    "최근 본 영화/드라마에 대해 감상을 이야기한다.",
    "주말에 갈 여행지를 검색하며 추천을 받는다.",
    "온라인 쇼핑 중 고객센터에 문의한다.",
]

OUTPUT_SCHEMA = [
    bigquery.SchemaField("run_id", "STRING"),
    bigquery.SchemaField("persona_id", "STRING"),
    bigquery.SchemaField("scenario", "STRING"),
    bigquery.SchemaField("messages", "JSON"),
    bigquery.SchemaField("model", "STRING"),
    bigquery.SchemaField("created_at", "TIMESTAMP"),
]


def scenarios() -> list[str]:
    raw = os.getenv("SCENARIOS", "").strip()
    if raw:
        return [s.strip() for s in raw.splitlines() if s.strip()]
    return DEFAULT_SCENARIOS


def sample_personas(bq: bigquery.Client) -> list[dict]:
    sql = f"SELECT * FROM `{PERSONAS_TABLE}` ORDER BY RAND() LIMIT {SAMPLE_SIZE}"
    return [dict(r) for r in bq.query(sql).result()]


def generate_dialogue(system_prompt: str, scenario: str) -> list[dict]:
    """페르소나(사용자 역) ↔ 상대(어시스턴트 역) 멀티턴 대화 생성."""
    messages: list[dict] = []
    # 사용자(페르소나)의 첫 발화
    opener = common.gemini_generate(
        system_prompt,
        f"상황: {scenario}\n이 상황에서 당신이 먼저 건넬 첫 마디를 한 문장으로 말하세요.",
    )
    messages.append({"role": "user", "content": opener})

    for _ in range(TURNS - 1):
        # 상대측 응답(중립 어시스턴트)
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        assistant = common.gemini_generate(
            "당신은 친절하고 도움이 되는 상대(점원/친구/상담원 등 상황에 맞는 역할)입니다. 한국어로 자연스럽게 한두 문장으로 응답하세요.",
            f"대화:\n{convo}\n\n다음 차례의 응답만 작성하세요.",
            temperature=0.7,
        )
        messages.append({"role": "assistant", "content": assistant})
        # 페르소나(사용자) 후속 발화
        convo = "\n".join(f"{m['role']}: {m['content']}" for m in messages)
        follow = common.gemini_generate(
            system_prompt,
            f"상황: {scenario}\n현재 대화:\n{convo}\n\n인물로서 다음 차례의 발화만 한두 문장으로 작성하세요.",
        )
        messages.append({"role": "user", "content": follow})
    return messages


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
    bq = bigquery.Client(location=BQ_LOCATION)
    ensure_table(bq)

    personas = sample_personas(bq)
    scen = scenarios()
    print(f"run {run_id}: 페르소나 {len(personas)} × 시나리오 {len(scen)} 생성 시작")

    rows: list[dict] = []
    now = dt.datetime.now(dt.timezone.utc).isoformat()
    for p in personas:
        system_prompt = common.build_system_prompt(p)
        pid = common.persona_id(p, fallback=str(uuid.uuid4()))
        for sc in scen:
            try:
                msgs = generate_dialogue(system_prompt, sc)
            except Exception as exc:  # noqa: BLE001
                print(f"  생성 실패(persona={pid}): {exc}")
                continue
            rows.append({
                "run_id": run_id,
                "persona_id": pid,
                "scenario": sc,
                "messages": json.dumps(msgs, ensure_ascii=False),
                "model": common.GEMINI_MODEL,
                "created_at": now,
            })
    print(f"생성 완료: {len(rows)} 대화")

    if not rows:
        return

    # GCS JSONL 백업
    with tempfile.TemporaryDirectory() as tmp:
        local = Path(tmp) / f"dialogues_{run_id}.jsonl"
        local.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows), encoding="utf-8")
        blob = f"persona_dialogues/{run_id}.jsonl"
        storage.Client().bucket(GCS_BUCKET).blob(blob).upload_from_filename(str(local))
        print(f"GCS 저장: gs://{GCS_BUCKET}/{blob}")

    # BigQuery append
    job = bq.load_table_from_json(
        rows, OUTPUT_TABLE,
        job_config=bigquery.LoadJobConfig(schema=OUTPUT_SCHEMA, write_disposition="WRITE_APPEND"),
    )
    job.result()
    print(f"BigQuery append 완료: {OUTPUT_TABLE} (+{len(rows)} rows)")


if __name__ == "__main__":
    main()
