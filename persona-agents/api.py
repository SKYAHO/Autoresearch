"""Cloud Run service: 페르소나 기반 가상 사용자와 인터랙티브 대화 API.

엔드포인트
  GET  /healthz                상태 확인
  GET  /personas?n=5           무작위 페르소나 샘플(요약)
  POST /chat                   페르소나로 응답 생성
       body: {
         "persona_id": "..."          # 또는
         "persona": { ...행 dict... }, # 직접 전달
         "message": "사용자 입력",
         "history": [{"role":"user|assistant","content":"..."}]
       }

환경변수
  GCP_PROJECT      (필수)
  PERSONAS_TABLE   (필수) project.dataset.personas
  BQ_LOCATION      기본 asia-northeast3
  GEMINI_MODEL / VERTEX_LOCATION  (common.py 참고)
"""

from __future__ import annotations

import os

from fastapi import FastAPI, HTTPException, Query
from pydantic import BaseModel, Field

import common

PERSONAS_TABLE = os.environ.get("PERSONAS_TABLE", "")
BQ_LOCATION = os.getenv("BQ_LOCATION", "asia-northeast3")

app = FastAPI(title="Persona Agents API", version="1.0.0")


def _bq():
    from google.cloud import bigquery

    return bigquery.Client(location=BQ_LOCATION)


class ChatRequest(BaseModel):
    persona_id: str | None = None
    persona: dict | None = None
    message: str
    history: list[dict] = Field(default_factory=list)


def fetch_persona(persona_id: str) -> dict:
    sql = f"SELECT * FROM `{PERSONAS_TABLE}` WHERE CAST(uuid AS STRING)=@id LIMIT 1"
    from google.cloud import bigquery

    job = _bq().query(
        sql,
        job_config=bigquery.QueryJobConfig(
            query_parameters=[bigquery.ScalarQueryParameter("id", "STRING", persona_id)]
        ),
    )
    rows = [dict(r) for r in job.result()]
    if not rows:
        raise HTTPException(404, f"persona_id not found: {persona_id}")
    return rows[0]


@app.get("/healthz")
def healthz() -> dict:
    return {"status": "ok", "model": common.GEMINI_MODEL}


@app.get("/personas")
def personas(n: int = Query(5, ge=1, le=50)) -> dict:
    from google.cloud import bigquery

    sql = f"SELECT * FROM `{PERSONAS_TABLE}` ORDER BY RAND() LIMIT @n"
    rows = [
        dict(r)
        for r in _bq().query(
            sql,
            job_config=bigquery.QueryJobConfig(
                query_parameters=[bigquery.ScalarQueryParameter("n", "INT64", n)]
            ),
        ).result()
    ]
    out = [{"persona_id": common.persona_id(r), "summary": common.build_system_prompt(r)[:300]} for r in rows]
    return {"count": len(out), "personas": out}


@app.post("/chat")
def chat(req: ChatRequest) -> dict:
    if req.persona is not None:
        row = req.persona
    elif req.persona_id:
        row = fetch_persona(req.persona_id)
    else:
        raise HTTPException(400, "persona 또는 persona_id 중 하나가 필요합니다.")

    system_prompt = common.build_system_prompt(row)
    convo = "\n".join(f"{m.get('role')}: {m.get('content')}" for m in req.history)
    contents = (f"이전 대화:\n{convo}\n\n" if convo else "") + f"상대: {req.message}\n인물로서 답하세요."
    reply = common.gemini_generate(system_prompt, contents)
    return {"persona_id": common.persona_id(row), "reply": reply}
