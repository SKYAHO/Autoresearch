"""공용 유틸: 페르소나 → system prompt 빌더, Vertex AI Gemini 호출.

환경변수
  GCP_PROJECT      (필수) GCP 프로젝트 ID
  VERTEX_LOCATION  Vertex AI 리전 (기본 us-central1; Gemini 가용 리전)
  GEMINI_MODEL     사용할 모델 (기본 gemini-2.5-flash)
"""

from __future__ import annotations

import os
from functools import lru_cache

GCP_PROJECT = os.getenv("GCP_PROJECT", "")
VERTEX_LOCATION = os.getenv("VERTEX_LOCATION", "us-central1")
GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

# 데이터셋에서 system prompt 로 엮을 후보 필드(있는 것만 사용)
PERSONA_TEXT_FIELDS = [
    "persona",
    "professional_persona",
    "sports_persona",
    "arts_persona",
    "travel_persona",
    "culinary_persona",
]
PERSONA_ATTR_FIELDS = [
    "age", "sex", "marital_status", "education_level",
    "occupation", "region", "city", "country",
]


def has_value(value) -> bool:
    if value is None:
        return False
    text = str(value).strip()
    return text != "" and text.lower() not in {"nan", "<na>", "none"}


def build_system_prompt(row: dict) -> str:
    """페르소나 한 행(dict) → 한국어 롤플레이 system prompt."""
    attrs = []
    for f in PERSONA_ATTR_FIELDS:
        v = row.get(f)
        if has_value(v):
            attrs.append(f"{f}: {v}")
    narrative = []
    for f in PERSONA_TEXT_FIELDS:
        v = row.get(f)
        if has_value(v):
            narrative.append(str(v).strip())

    lines = [
        "당신은 아래에 묘사된 한국의 한 가상 인물입니다. 이 인물의 성격, 가치관, 말투, 생활 맥락을",
        "일관되게 유지하며 실제 사용자처럼 자연스럽게 한국어로 행동/응답하세요.",
        "AI라는 사실을 언급하지 말고, 인물 1인칭으로 답하세요.",
        "",
        "## 인물 속성",
    ]
    lines.append(", ".join(attrs) if attrs else "(속성 정보 없음)")
    if narrative:
        lines += ["", "## 인물 서사", *narrative]
    return "\n".join(lines)


def persona_id(row: dict, fallback: str = "") -> str:
    for k in ("uuid", "persona_id", "id"):
        if has_value(row.get(k)):
            return str(row[k])
    return fallback


@lru_cache(maxsize=1)
def _client():
    # google-genai SDK 를 Vertex AI 백엔드로 사용
    from google import genai

    if not GCP_PROJECT:
        raise SystemExit("오류: GCP_PROJECT 환경변수가 필요합니다.")
    return genai.Client(vertexai=True, project=GCP_PROJECT, location=VERTEX_LOCATION)


def gemini_generate(
    system_prompt: str,
    contents,
    temperature: float = 0.9,
    response_mime_type: str | None = None,
    max_output_tokens: int = 2048,
) -> str:
    """system_prompt + contents → 생성 텍스트.

    response_mime_type="application/json" 이면 모델이 JSON 으로만 응답한다.
    """
    from google.genai import types

    client = _client()
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=max_output_tokens,
            response_mime_type=response_mime_type,
        ),
    )
    return (resp.text or "").strip()


def parse_json_array(text: str):
    """모델 응답에서 JSON 배열을 견고하게 추출."""
    import json
    import re

    t = text.strip()
    if t.startswith("```"):
        t = re.sub(r"^```[a-zA-Z]*\n?", "", t).rstrip("`").strip()
    try:
        return json.loads(t)
    except Exception:
        m = re.search(r"\[.*\]", t, re.DOTALL)
        if m:
            return json.loads(m.group(0))
        raise
