"""VirtualUser × 후보 영상 batch를 받아 후보별 클릭 판단(judgments)을 생성한다.

역할 분담: LLM은 실제 title/description을 읽고 후보별 click_propensity/watch_fraction만
판단한다. would_like 파생·전역 2% 정규화·timestamp·제약 강제는 pipeline(코드).
"""
import hashlib
import json
import logging
import os
import random
import re
import threading
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from email.utils import parsedate_to_datetime
from typing import Literal

from openai import APITimeoutError

from autoresearch.action_logs.candidate import (
    _relevance_score,
    _user_keywords,
    _video_text,
)
from autoresearch.action_logs.observability import emit_action_log_event
from autoresearch.action_logs.schema import PROMPT_VERSION


logger = logging.getLogger(__name__)

DEFAULT_OPENROUTER_MODEL = "mistralai/mistral-nemo"
DEFAULT_OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
DEFAULT_OPENROUTER_TIMEOUT_SEC = 60.0
DEFAULT_OPENROUTER_MAX_RETRIES = 2
DEFAULT_OPENROUTER_TIMEOUT_MAX_RETRIES = 1
DEFAULT_OPENROUTER_RETRY_BACKOFF_BASE_SEC = 1.0
DEFAULT_OPENROUTER_RETRY_BACKOFF_MAX_SEC = 30.0
_RETRYABLE_STATUS_CODES = frozenset({408, 429, 502, 503, 504})
_PROVIDER_ROUTING_MODES = frozenset({"default", "auto", "fixed"})
_PROVIDER_SLUG_PATTERN = re.compile(
    r"[a-z0-9]+(?:-[a-z0-9]+)*(?:/[a-z0-9]+(?:-[a-z0-9]+)*)*"
)
_SAFE_PROVIDER_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9 ._:/()+-]{0,127}")
_OPENROUTER_METADATA_HEADER = {"X-OpenRouter-Metadata": "enabled"}

ACTION_LOG_SYSTEM_HARNESS = """너는 한국 YouTube 사용자의 시청 행동을 시뮬레이션하는 판정기다.
주어진 사용자 프로필과 후보 영상 목록을 근거로, 각 후보 영상에 대해 이 사용자가
클릭할 가능성을 판단한다.
- click_propensity: 0~1. 사용자 관심사와 영상 내용(title/description/tags)이 맞을수록 높다.
- watch_fraction: 0~1. 클릭했다고 가정할 때 영상을 얼마나 볼지 비율.
지어내지 말고 프로필과 영상 텍스트에 근거해 판단하라. 대부분의 영상은 관심 밖이라
click_propensity가 낮아야 정상이다.
출력은 판정을 담은 JSON 객체 {"j": [[index, click_propensity, watch_fraction], ...]}
하나만 허용한다. index는 후보의 0-base 배열 위치다. 최상위는 반드시 객체다.
Markdown이나 주석을 넣지 마라."""


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


CANDIDATE_COLUMNS = "[title, tags, channel, description]"


def _candidate_block(videos: list[dict]) -> str:
    """프롬프트에 넣을 후보 영상 목록.

    토큰 절약: 반복 키를 제거한 위치기반 배열-of-배열로 직렬화한다. 배열 위치가
    후보 index이며, 컬럼 순서는 CANDIDATE_COLUMNS(title, tags, channel, description).
    video_id는 넣지 않고(응답도 위치로 정렬) 필드 truncation 한도는 유지한다.
    """

    rows = []
    for v in videos:
        rows.append(
            [
                str(v.get("title", ""))[:120],
                (v.get("tags") or [])[:8],
                str(v.get("channel_name", ""))[:40],
                str(v.get("description", ""))[:160],
            ]
        )
    return json.dumps(rows, ensure_ascii=False)


def build_action_log_prompt(virtual_user: dict, videos: list[dict]) -> str:
    """사용자 프로필 + 후보 영상 목록을 판정 요청 프롬프트로 만든다."""

    n = len(videos)
    return f"""Prompt version: {PROMPT_VERSION}

사용자 프로필:
{_user_profile_block(virtual_user)}

후보 영상({n}개, 배열 위치 = 후보 index):
컬럼 순서 = {CANDIDATE_COLUMNS}
{_candidate_block(videos)}

각 후보를 판단해 아래 JSON만 출력하라(주석/Markdown 금지):
{{"j": [[index, click_propensity, watch_fraction], ...]}}

제약:
- index 는 후보의 0-base 배열 위치(위 목록 순서)다. 0~{n - 1}을 각 1회씩, 정확히 {n}개.
- 각 원소는 [index, click_propensity, watch_fraction] 형태의 길이 3 배열.
- click_propensity, watch_fraction 은 0~1 사이 실수.
"""


class RuleBasedActionLogGenerator:
    """LLM 없이 결정론적으로 judgments JSON을 만드는 fixture generator."""

    def __init__(self, model_name: str = "fixture-rule-action-log") -> None:
        self.model_name = model_name

    def generate(self, virtual_user: dict, videos: list[dict]) -> str:
        """유저 키워드-영상 텍스트 겹침으로 결정론적 판단을 만든다.

        출력은 LLM generator와 동일한 인덱스 포맷({"j": [[idx, cp, wf], ...]})이다.
        would_like는 출력하지 않고 파이프라인 파싱에서 코드로 파생한다.
        """

        keywords = _user_keywords(virtual_user)
        rows = []
        for i, v in enumerate(videos):
            overlap = _relevance_score(keywords, _video_text(v))
            jitter = (int(hashlib.sha256(v["video_id"].encode()).hexdigest(), 16) % 100) / 1000.0
            propensity = min(1.0, 0.05 + 0.25 * overlap + jitter)
            rows.append(
                [i, round(propensity, 3), round(min(1.0, propensity + 0.1), 3)]
            )
        return json.dumps({"j": rows}, ensure_ascii=False)


def _env_float(name: str, default: float) -> float:
    value = os.environ.get(name)
    return float(value) if value not in {None, ""} else default


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value not in {None, ""} else default


def _env_optional_bool(name: str) -> bool | None:
    value = os.environ.get(name)
    if value in {None, ""}:
        return None
    normalized = value.strip().lower()
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} must be a boolean")


def _normalize_provider_routing(
    provider_routing_mode: str,
    provider_slug: str | None,
) -> tuple[str, str | None]:
    """OpenRouter routing mode와 fixed provider slug를 fail-closed 정규화한다."""

    if not isinstance(provider_routing_mode, str):
        raise ValueError(
            "provider_routing_mode must be one of: default, auto, fixed"
        )
    normalized_mode = provider_routing_mode
    if normalized_mode not in _PROVIDER_ROUTING_MODES:
        raise ValueError(
            "provider_routing_mode must be one of: default, auto, fixed"
        )

    if normalized_mode != "fixed":
        if provider_slug is not None:
            raise ValueError(
                "provider_slug is only allowed when provider_routing_mode='fixed'"
            )
        return normalized_mode, None

    if not isinstance(provider_slug, str) or not provider_slug.strip():
        raise ValueError(
            "provider_slug is required when provider_routing_mode='fixed'"
        )
    normalized_slug = provider_slug.strip().lower()
    if (
        len(normalized_slug) > 128
        or _PROVIDER_SLUG_PATTERN.fullmatch(normalized_slug) is None
    ):
        raise ValueError("provider_slug must be a valid OpenRouter provider slug")
    return normalized_mode, normalized_slug


class OpenRouterRequestError(RuntimeError):
    """노출 가능한 구조화 필드만 보존한 OpenRouter 최종 요청 실패."""

    def __init__(
        self,
        *,
        status: int | None,
        error_type: str,
        provider: str,
        attempts: int,
    ) -> None:
        self.status = status
        self.error_type = error_type
        self.provider = provider
        self.attempts = attempts
        super().__init__(
            "OpenRouter request failed "
            f"(status={status}, error_type={error_type}, "
            f"provider={provider}, attempts={attempts})"
        )

    @property
    def log_fields(self) -> dict[str, object]:
        """시크릿·요청·응답 본문을 제외한 구조화 로그 필드."""

        return {
            "status": self.status,
            "error_type": self.error_type,
            "provider": self.provider,
            "attempts": self.attempts,
        }


class OpenRouterActionLogGenerator:
    """OpenAI-compatible OpenRouter API로 judgments를 생성하는 generator."""

    def __init__(
        self,
        api_key: str | None = None,
        model_name: str = DEFAULT_OPENROUTER_MODEL,
        base_url: str | None = None,
        max_tokens: int = 4000,
        timeout_seconds: float | None = None,
        max_retries: int | None = None,
        timeout_max_retries: int | None = None,
        retry_backoff_base_seconds: float | None = None,
        retry_backoff_max_seconds: float | None = None,
        provider_sort: Literal["price", "throughput", "latency"] | None = None,
        allow_fallbacks: bool | None = None,
        require_parameters: bool | None = None,
        provider_routing_mode: str = "default",
        provider_slug: str | None = None,
    ) -> None:
        self.provider_routing_mode, self.provider_slug = _normalize_provider_routing(
            provider_routing_mode,
            provider_slug,
        )
        self.api_key = api_key or os.environ.get("OPENROUTER_API_KEY")
        self.base_url = base_url or DEFAULT_OPENROUTER_BASE_URL
        self.model_name = model_name
        self.max_tokens = max_tokens
        self.timeout_seconds = (
            timeout_seconds
            if timeout_seconds is not None
            else _env_float(
                "OPENROUTER_TIMEOUT_SEC",
                DEFAULT_OPENROUTER_TIMEOUT_SEC,
            )
        )
        self.max_retries = (
            max_retries
            if max_retries is not None
            else _env_int("OPENROUTER_MAX_RETRIES", DEFAULT_OPENROUTER_MAX_RETRIES)
        )
        self.timeout_max_retries = (
            timeout_max_retries
            if timeout_max_retries is not None
            else _env_int(
                "OPENROUTER_TIMEOUT_MAX_RETRIES",
                DEFAULT_OPENROUTER_TIMEOUT_MAX_RETRIES,
            )
        )
        self.retry_backoff_base_seconds = (
            retry_backoff_base_seconds
            if retry_backoff_base_seconds is not None
            else _env_float(
                "OPENROUTER_RETRY_BACKOFF_BASE_SEC",
                DEFAULT_OPENROUTER_RETRY_BACKOFF_BASE_SEC,
            )
        )
        self.retry_backoff_max_seconds = (
            retry_backoff_max_seconds
            if retry_backoff_max_seconds is not None
            else _env_float(
                "OPENROUTER_RETRY_BACKOFF_MAX_SEC",
                DEFAULT_OPENROUTER_RETRY_BACKOFF_MAX_SEC,
            )
        )
        if self.provider_routing_mode == "default":
            self.provider_sort = (
                provider_sort
                or os.environ.get("OPENROUTER_PROVIDER_SORT")
                or None
            )
            self.allow_fallbacks = (
                allow_fallbacks
                if allow_fallbacks is not None
                else _env_optional_bool("OPENROUTER_ALLOW_FALLBACKS")
            )
            self.require_parameters = (
                require_parameters
                if require_parameters is not None
                else _env_optional_bool("OPENROUTER_REQUIRE_PARAMETERS")
            )
        else:
            self.provider_sort = None
            self.allow_fallbacks = (
                False if self.provider_routing_mode == "fixed" else None
            )
            self.require_parameters = None
        self._thread_local = threading.local()
        self._client_lock = threading.Lock()
        self._clients: list[object] = []
        self._closed = False
        if not self.api_key:
            raise ValueError("OPENROUTER_API_KEY is required for OpenRouterActionLogGenerator")
        if self.timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be greater than 0")
        if self.max_retries < 0:
            raise ValueError("max_retries must be at least 0")
        if self.timeout_max_retries < 0:
            raise ValueError("timeout_max_retries must be at least 0")
        if self.retry_backoff_base_seconds < 0:
            raise ValueError("retry_backoff_base_seconds must be at least 0")
        if self.retry_backoff_max_seconds < self.retry_backoff_base_seconds:
            raise ValueError(
                "retry_backoff_max_seconds must be at least retry_backoff_base_seconds"
            )
        if self.provider_sort not in {None, "price", "throughput", "latency"}:
            raise ValueError("provider_sort must be one of: price, throughput, latency")

    def _client_kwargs(self) -> dict[str, object]:
        return {
            "api_key": self.api_key,
            "base_url": self.base_url,
            "timeout": self.timeout_seconds,
            "max_retries": 0,
        }

    @property
    def fingerprint_config(self) -> dict[str, object]:
        """LLM 출력과 provider 선택에 영향을 주는 안전한 설정."""

        return {
            "base_url_fingerprint": hashlib.sha256(self.base_url.encode()).hexdigest(),
            "max_tokens": self.max_tokens,
            "provider_routing_mode": self.provider_routing_mode,
            "provider_slug": self.provider_slug,
            "provider_preferences": self._provider_preferences(),
            "provider_sort": self.provider_sort,
            "allow_fallbacks": self.allow_fallbacks,
            "require_parameters": self.require_parameters,
        }

    def _get_client(self):
        """현재 worker thread 전용 OpenAI client와 connection pool을 재사용한다."""

        with self._client_lock:
            if self._closed:
                raise RuntimeError("OpenRouterActionLogGenerator is closed")
            client = getattr(self._thread_local, "client", None)
            if client is None:
                from openai import OpenAI

                client = OpenAI(**self._client_kwargs())
                self._thread_local.client = client
                self._clients.append(client)
            return client

    def _provider_preferences(self) -> dict[str, object]:
        if self.provider_routing_mode == "auto":
            return {}
        if self.provider_routing_mode == "fixed":
            assert self.provider_slug is not None
            return {
                "only": [self.provider_slug],
                "allow_fallbacks": False,
            }

        preferences: dict[str, object] = {}
        if self.provider_sort is not None:
            preferences["sort"] = self.provider_sort
        if self.allow_fallbacks is not None:
            preferences["allow_fallbacks"] = self.allow_fallbacks
        if self.require_parameters is not None:
            preferences["require_parameters"] = self.require_parameters
        return preferences

    @staticmethod
    def _status_code(exc: Exception) -> int | None:
        status = getattr(exc, "status_code", None)
        return int(status) if isinstance(status, int) else None

    @staticmethod
    def _response_headers(exc: Exception):
        response = getattr(exc, "response", None)
        return getattr(response, "headers", {}) if response is not None else {}

    @classmethod
    def _provider_name(cls, exc: Exception) -> str:
        headers = cls._response_headers(exc)
        for name in ("x-openrouter-provider", "x-provider"):
            value = headers.get(name) if hasattr(headers, "get") else None
            if value:
                return str(value)
        return "unknown"

    @staticmethod
    def _safe_provider_name(value: object) -> str | None:
        """외부 metadata에서 로그 가능한 짧은 provider 이름만 허용한다."""

        if not isinstance(value, str):
            return None
        normalized = value.strip()
        if _SAFE_PROVIDER_NAME_PATTERN.fullmatch(normalized) is None:
            return None
        return normalized

    @staticmethod
    def _safe_nonnegative_int(value: object) -> int | None:
        """bool과 비정상 값을 제외하고 외부 metadata 숫자를 정수로 읽는다."""

        if isinstance(value, bool):
            return None
        if isinstance(value, int):
            parsed = value
        elif isinstance(value, float) and value.is_integer():
            parsed = int(value)
        elif isinstance(value, str) and value.strip().isdigit():
            parsed = int(value.strip())
        else:
            return None
        return parsed if parsed >= 0 else None

    @staticmethod
    def _router_metadata(response: object) -> Mapping[str, object] | None:
        """SDK model_extra의 공식 router metadata object만 선택한다."""

        model_extra = getattr(response, "model_extra", None)
        if not isinstance(model_extra, Mapping):
            return None
        metadata = model_extra.get("openrouter_metadata")
        return metadata if isinstance(metadata, Mapping) else None

    @classmethod
    def _router_metadata_fields(
        cls,
        response: object,
    ) -> dict[str, int | str]:
        """Router metadata 원문 없이 선택 provider와 안전한 count만 반환한다."""

        metadata = cls._router_metadata(response)
        if metadata is None:
            return {}

        selected_provider = None
        endpoints = metadata.get("endpoints")
        if isinstance(endpoints, Mapping):
            available = endpoints.get("available")
            if isinstance(available, (list, tuple)):
                for endpoint in available:
                    if not isinstance(endpoint, Mapping):
                        continue
                    if endpoint.get("selected") is not True:
                        continue
                    selected_provider = cls._safe_provider_name(
                        endpoint.get("provider")
                    )
                    if selected_provider is not None:
                        break

        attempts_value = metadata.get("attempts")
        attempts: list[Mapping[str, object]] | None = None
        if isinstance(attempts_value, (list, tuple)):
            attempts = [
                attempt
                for attempt in attempts_value
                if isinstance(attempt, Mapping)
            ]

        if selected_provider is None and attempts:
            for attempt in reversed(attempts):
                status = cls._safe_nonnegative_int(attempt.get("status"))
                if status is not None and not 200 <= status < 300:
                    continue
                selected_provider = cls._safe_provider_name(
                    attempt.get("provider")
                )
                if selected_provider is not None:
                    break

        attempt_count = len(attempts) if attempts is not None else None
        reported_attempt = cls._safe_nonnegative_int(metadata.get("attempt"))
        if reported_attempt is not None:
            attempt_count = max(attempt_count or 0, reported_attempt)

        fields: dict[str, int | str] = {}
        if selected_provider is not None:
            fields["provider"] = selected_provider
        if attempt_count is not None and attempt_count > 0:
            fields["router_attempt_count"] = attempt_count
            fields["router_fallback_count"] = max(0, attempt_count - 1)
        if attempts is not None:
            fields["router_429_count"] = sum(
                cls._safe_nonnegative_int(attempt.get("status")) == 429
                for attempt in attempts
            )
        return fields

    @classmethod
    def _response_provider(cls, response: object) -> str:
        """공식 metadata를 우선하고 기존 response provider fallback을 유지한다."""

        metadata_provider = cls._router_metadata_fields(response).get("provider")
        if isinstance(metadata_provider, str):
            return metadata_provider

        provider = getattr(response, "provider", None)
        safe_provider = cls._safe_provider_name(provider)
        if safe_provider is not None:
            return safe_provider
        model_extra = getattr(response, "model_extra", None)
        if isinstance(model_extra, Mapping):
            safe_provider = cls._safe_provider_name(model_extra.get("provider"))
            if safe_provider is not None:
                return safe_provider
        return "unknown"

    @staticmethod
    def _usage_fields(response: object) -> dict[str, int | float]:
        """응답 본문 없이 token/reasoning/cost 메타데이터만 반환한다."""

        usage = getattr(response, "usage", None)
        if usage is None:
            return {}
        fields: dict[str, int | float] = {}
        for source_name, target_name in (
            ("prompt_tokens", "prompt_tokens"),
            ("completion_tokens", "completion_tokens"),
            ("cost", "reported_cost"),
        ):
            value = getattr(usage, source_name, None)
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                fields[target_name] = value
        details = getattr(usage, "completion_tokens_details", None)
        reasoning_tokens = getattr(details, "reasoning_tokens", None)
        if isinstance(reasoning_tokens, int) and not isinstance(reasoning_tokens, bool):
            fields["reasoning_tokens"] = reasoning_tokens
        return fields

    @classmethod
    def _retry_after_seconds(cls, exc: Exception) -> float | None:
        headers = cls._response_headers(exc)
        value = headers.get("retry-after") if hasattr(headers, "get") else None
        if value in {None, ""}:
            return None
        try:
            return max(0.0, float(value))
        except (TypeError, ValueError):
            try:
                retry_at = parsedate_to_datetime(str(value))
            except (TypeError, ValueError, OverflowError):
                return None
            if retry_at.tzinfo is None:
                retry_at = retry_at.replace(tzinfo=UTC)
            return max(0.0, (retry_at - datetime.now(UTC)).total_seconds())

    def _retry_delay(self, exc: Exception, attempt: int) -> float:
        backoff = min(
            self.retry_backoff_max_seconds,
            self.retry_backoff_base_seconds * (2 ** (attempt - 1)),
        )
        retry_after = self._retry_after_seconds(exc) or 0.0
        jitter = random.uniform(0.0, self.retry_backoff_base_seconds)
        return max(backoff, retry_after) + jitter

    def close(self) -> None:
        """생성한 모든 thread-local client의 connection pool을 닫는다."""

        with self._client_lock:
            if self._closed:
                return
            self._closed = True
            clients = list({id(client): client for client in self._clients}.values())
            self._clients.clear()
        for client in clients:
            close = getattr(client, "close", None)
            if callable(close):
                close()

    def __enter__(self) -> "OpenRouterActionLogGenerator":
        return self

    def __exit__(self, exc_type, exc, traceback) -> None:
        self.close()

    def generate(self, virtual_user: dict, videos: list[dict]) -> str:
        """OpenRouter에 판정 요청을 보내고 raw response text를 반환한다."""

        request: dict[str, object] = {
            "model": self.model_name,
            "messages": [
                {"role": "system", "content": ACTION_LOG_SYSTEM_HARNESS},
                {"role": "user", "content": build_action_log_prompt(virtual_user, videos)},
            ],
            "response_format": {"type": "json_object"},
            "max_tokens": self.max_tokens,
            "extra_headers": dict(_OPENROUTER_METADATA_HEADER),
        }
        provider = self._provider_preferences()
        if provider:
            request["extra_body"] = {"provider": provider}

        client = self._get_client()
        response = None
        attempts = 0
        total_retries = 0
        timeout_retries = 0
        request_started_at = time.monotonic()
        while response is None:
            attempts += 1
            attempt_started_at = time.monotonic()
            try:
                response = client.chat.completions.create(**request)
            except Exception as exc:  # noqa: BLE001 - SDK exception boundary
                attempt_elapsed_ms = (time.monotonic() - attempt_started_at) * 1000
                status = self._status_code(exc)
                is_timeout = isinstance(exc, APITimeoutError)
                is_retryable = is_timeout or status in _RETRYABLE_STATUS_CODES
                can_retry_timeout = (
                    not is_timeout or timeout_retries < self.timeout_max_retries
                )
                if (
                    is_retryable
                    and total_retries < self.max_retries
                    and can_retry_timeout
                ):
                    total_retries += 1
                    if is_timeout:
                        timeout_retries += 1
                    delay = self._retry_delay(exc, attempts)
                    provider_name = self._provider_name(exc)
                    emit_action_log_event(
                        logger,
                        logging.WARNING,
                        "openrouter_retry_scheduled",
                        attempt=attempts,
                        retry_count=total_retries,
                        backoff_seconds=round(delay, 3),
                        http_status=status if status is not None else 0,
                        provider=provider_name,
                        request_elapsed_ms=round(
                            (time.monotonic() - request_started_at) * 1000,
                            3,
                        ),
                    )
                    backoff_started_at = time.monotonic()
                    time.sleep(delay)
                    backoff_elapsed_ms = (
                        time.monotonic() - backoff_started_at
                    ) * 1000
                    emit_action_log_event(
                        logger,
                        logging.WARNING,
                        "openrouter_attempt_complete",
                        attempt=attempts,
                        retry_count=total_retries,
                        http_status=status if status is not None else 0,
                        error_type=type(exc).__name__,
                        provider=provider_name,
                        attempt_elapsed_ms=round(attempt_elapsed_ms, 3),
                        backoff_scheduled_ms=round(delay * 1000, 3),
                        backoff_elapsed_ms=round(backoff_elapsed_ms, 3),
                        outcome="retry",
                    )
                    continue
                error = OpenRouterRequestError(
                    status=status,
                    error_type=type(exc).__name__,
                    provider=self._provider_name(exc),
                    attempts=attempts,
                )
                request_elapsed_ms = (time.monotonic() - request_started_at) * 1000
                emit_action_log_event(
                    logger,
                    logging.ERROR,
                    "openrouter_attempt_complete",
                    attempt=attempts,
                    retry_count=total_retries,
                    http_status=status if status is not None else 0,
                    error_type=type(exc).__name__,
                    provider=error.provider,
                    attempt_elapsed_ms=round(attempt_elapsed_ms, 3),
                    backoff_scheduled_ms=0.0,
                    backoff_elapsed_ms=0.0,
                    outcome="failed",
                )
                emit_action_log_event(
                    logger,
                    logging.ERROR,
                    "openrouter_request_complete",
                    request_elapsed_ms=round(request_elapsed_ms, 3),
                    retry_count=total_retries,
                    attempt=attempts,
                    http_status=status if status is not None else 0,
                    provider=error.provider,
                    outcome="failed",
                )
                raise error from None
            else:
                provider_name = self._response_provider(response)
                emit_action_log_event(
                    logger,
                    logging.INFO,
                    "openrouter_attempt_complete",
                    detailed_only=True,
                    attempt=attempts,
                    retry_count=total_retries,
                    http_status=200,
                    provider=provider_name,
                    attempt_elapsed_ms=round(
                        (time.monotonic() - attempt_started_at) * 1000,
                        3,
                    ),
                    backoff_scheduled_ms=0.0,
                    backoff_elapsed_ms=0.0,
                    outcome="success",
                )

        assert response is not None
        router_fields = self._router_metadata_fields(response)
        provider_name = str(
            router_fields.pop("provider", self._response_provider(response))
        )
        emit_action_log_event(
            logger,
            logging.INFO,
            "openrouter_request_complete",
            detailed_only=True,
            request_elapsed_ms=round(
                (time.monotonic() - request_started_at) * 1000,
                3,
            ),
            retry_count=total_retries,
            attempt=attempts,
            http_status=200,
            provider=provider_name,
            outcome="success",
            **self._usage_fields(response),
            **router_fields,
        )
        return response.choices[0].message.content or ""
