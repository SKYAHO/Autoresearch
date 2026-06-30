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


def build_system_prompt(row: dict) -> str:
    """페르소나 한 행(dict) → 한국어 롤플레이 system prompt."""
    attrs = []
    for f in PERSONA_ATTR_FIELDS:
        v = row.get(f)
        if v not in (None, "", "nan"):
            attrs.append(f"{f}: {v}")
    narrative = []
    for f in PERSONA_TEXT_FIELDS:
        v = row.get(f)
        if v not in (None, "", "nan"):
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
        if row.get(k):
            return str(row[k])
    return fallback


@lru_cache(maxsize=1)
def _client():
    # google-genai SDK 를 Vertex AI 백엔드로 사용
    from google import genai

    if not GCP_PROJECT:
        raise SystemExit("오류: GCP_PROJECT 환경변수가 필요합니다.")
    return genai.Client(vertexai=True, project=GCP_PROJECT, location=VERTEX_LOCATION)


def gemini_generate(system_prompt: str, contents, temperature: float = 0.9) -> str:
    """system_prompt + contents(문자열 또는 메시지 리스트) → 생성 텍스트."""
    from google.genai import types

    client = _client()
    resp = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=contents,
        config=types.GenerateContentConfig(
            system_instruction=system_prompt,
            temperature=temperature,
            max_output_tokens=1024,
        ),
    )
    return (resp.text or "").strip()
