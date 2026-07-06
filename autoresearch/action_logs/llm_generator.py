"""VirtualUser × 후보 영상 batch를 받아 후보별 클릭 판단(judgments)을 생성한다.

역할 분담: LLM은 실제 title/description을 읽고 후보별 click_propensity/watch_fraction/
would_like만 판단한다. 전역 2% 정규화·timestamp·제약 강제는 pipeline(코드).
"""
import hashlib
import json
import logging
import os

from autoresearch.action_logs.candidate import (
    _relevance_score,
    _user_keywords,
    _video_text,
)
from autoresearch.action_logs.schema import PROMPT_VERSION


logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_MODEL = "mistralai/mistral-nemo"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"

ACTION_LOG_SYSTEM_HARNESS = """너는 한국 YouTube 사용자의 시청 행동을 시뮬레이션하는 판정기다.
주어진 사용자 프로필과 후보 영상 목록을 근거로, 각 후보 영상에 대해 이 사용자가
클릭할 가능성을 판단한다.
- click_propensity: 0~1. 사용자 관심사와 영상 내용(title/description/tags)이 맞을수록 높다.
- watch_fraction: 0~1. 클릭했다고 가정할 때 영상을 얼마나 볼지 비율.
- would_like: true/false. 클릭 후 좋아요를 누를 만큼 만족할지.
지어내지 말고 프로필과 영상 텍스트에 근거해 판단하라. 대부분의 영상은 관심 밖이라
click_propensity가 낮아야 정상이다.
출력은 지정된 JSON 하나만 허용한다. Markdown이나 주석을 넣지 마라."""


def _user_profile_block(virtual_user: dict) -> str:
    """프롬프트에 넣을 사용자 프로필 요약."""

    affinity = virtual_user.get("category_affinity") or {}
    top_cats = sorted(affinity.items(), key=lambda kv: -float(kv[1]))[:5] if isinstance(affinity, dict) else []
    return json.dumps(
        {
            "age": virtual_user.get("age"),
            "sex": virtual_user.get("sex"),
            "persona_summary": virtual_user.get("persona_summary", ""),
            "primary_categories": virtual_user.get("primary_categories", []),
            "top_category_affinity": {k: round(float(v), 2) for k, v in top_cats},
            "interest_keywords": virtual_user.get("interest_keywords", []),
            "hobby_keywords": virtual_user.get("hobby_keywords", []),
            "watch_time_band": virtual_user.get("watch_time_band", ""),
        },
        ensure_ascii=False,
    )


def _candidate_block(videos: list[dict]) -> str:
    """프롬프트에 넣을 후보 영상 목록(토큰 절약을 위해 필드 축약)."""

    items = []
    for v in videos:
        items.append(
            {
                "video_id": v["video_id"],
                "title": str(v.get("title", ""))[:120],
                "tags": (v.get("tags") or [])[:8],
                "channel": str(v.get("channel_name", ""))[:40],
                "description": str(v.get("description", ""))[:160],
            }
        )
    return json.dumps(items, ensure_ascii=False)


def build_action_log_prompt(virtual_user: dict, videos: list[dict]) -> str:
    """사용자 프로필 + 후보 영상 목록을 판정 요청 프롬프트로 만든다."""

    return f"""Prompt version: {PROMPT_VERSION}

사용자 프로필:
{_user_profile_block(virtual_user)}

후보 영상 목록({len(videos)}개):
{_candidate_block(videos)}

각 후보 영상 video_id마다 판단을 반환하라. 아래 JSON 형태만 출력하라(주석/Markdown 금지):
{{"judgments": [
  {{"video_id": "...", "click_propensity": 0.0, "watch_fraction": 0.0, "would_like": false}}
]}}

제약:
- judgments는 후보 영상 전체를 포함한다(각 video_id 1회).
- click_propensity, watch_fraction 은 0~1 사이 실수.
- would_like 은 true/false.
"""


class RuleBasedActionLogGenerator:
    """LLM 없이 결정론적으로 judgments JSON을 만드는 fixture generator."""

    def __init__(self, model_name: str = "fixture-rule-action-log") -> None:
        self.model_name = model_name

    def generate(self, virtual_user: dict, videos: list[dict]) -> str:
        """유저 키워드-영상 텍스트 겹침으로 결정론적 판단을 만든다."""

        keywords = _user_keywords(virtual_user)
        judgments = []
        for v in videos:
            overlap = _relevance_score(keywords, _video_text(v))
            jitter = (int(hashlib.sha256(v["video_id"].encode()).hexdigest(), 16) % 100) / 1000.0
            propensity = min(1.0, 0.05 + 0.25 * overlap + jitter)
            judgments.append(
                {
                    "video_id": v["video_id"],
                    "click_propensity": round(propensity, 3),
                    "watch_fraction": round(min(1.0, propensity + 0.1), 3),
                    "would_like": propensity > 0.7,
                }
            )
        return json.dumps({"judgments": judgments}, ensure_ascii=False)


class OpenRouterActionLogGenerator:
    """OpenAI-compatible OpenRouter API로 judgments를 생성하는 generator."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = DEFAULT_OPENROUTER_MODEL,
        base_url: str | None = None,
        max_tokens: int = 4000,
    ) -> None:
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.base_url = base_url or DEFAULT_OPENROUTER_BASE_URL
        self.model_name = model_name
        self.max_tokens = max_tokens
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is required for OpenRouterActionLogGenerator")

    def _client_kwargs(self) -> dict[str, object]:
        return {"api_key": self.api_key, "base_url": self.base_url}

    def generate(self, virtual_user: dict, videos: list[dict]) -> str:
        """OpenRouter에 판정 요청을 보내고 raw response text를 반환한다."""

        from openai import OpenAI

        client = OpenAI(**self._client_kwargs())
        response = client.chat.completions.create(
            model=self.model_name,
            messages=[
                {"role": "system", "content": ACTION_LOG_SYSTEM_HARNESS},
                {"role": "user", "content": build_action_log_prompt(virtual_user, videos)},
            ],
            response_format={"type": "json_object"},
            max_tokens=self.max_tokens,
        )
        logger.info(
            "Generated action log judgments",
            extra={"user_id": virtual_user.get("user_id"), "model_name": self.model_name},
        )
        return response.choices[0].message.content or ""
