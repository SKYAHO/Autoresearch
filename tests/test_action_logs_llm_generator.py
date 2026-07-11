import json
import logging
from concurrent.futures import ThreadPoolExecutor
from threading import Barrier
from types import SimpleNamespace

import httpx
import pytest
from openai import APITimeoutError

import autoresearch.action_logs.llm_generator as llm_module
from autoresearch.action_logs.llm_generator import (
    OpenRouterActionLogGenerator,
    OpenRouterRequestError,
)
from autoresearch.action_logs.observability import action_log_work_log_context


def _user():
    return {
        "user_id": "vu_test",
        "primary_categories": ["Gaming"],
        "interest_keywords": ["게임"],
    }


def _videos():
    return [
        {
            "video_id": "video_test",
            "title": "테스트 영상",
            "description": "설명",
            "tags": ["게임"],
        }
    ]


def _success(content: str = '{"judgments": []}', **kwargs):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))],
        **kwargs,
    )


def _timeout_error() -> APITimeoutError:
    return APITimeoutError(
        request=httpx.Request("POST", "https://openrouter.ai/api/v1/chat/completions")
    )


class _StatusError(Exception):
    def __init__(self, status_code: int, headers: dict[str, str] | None = None):
        super().__init__(f"unsafe upstream detail for {status_code}")
        self.status_code = status_code
        self.response = SimpleNamespace(headers=headers or {})


class _FakeCompletions:
    def __init__(self, outcomes=None):
        self.outcomes = list(outcomes or [])
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        outcome = self.outcomes.pop(0) if self.outcomes else _success()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


class _FakeClient:
    def __init__(self, outcomes=None):
        self.completions = _FakeCompletions(outcomes)
        self.chat = SimpleNamespace(completions=self.completions)
        self.closed = False

    def close(self):
        self.closed = True


def test_openrouter_reuses_thread_local_clients_and_closes_lifecycle(monkeypatch):
    clients = []
    client_kwargs = []

    def _factory(**kwargs):
        client = _FakeClient()
        clients.append(client)
        client_kwargs.append(kwargs)
        return client

    monkeypatch.setattr("openai.OpenAI", _factory)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        timeout_seconds=12.5,
    )

    generator.generate(_user(), _videos())
    generator.generate(_user(), _videos())
    barrier = Barrier(3)

    def _generate_in_worker():
        barrier.wait()
        return generator.generate(_user(), _videos())

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(_generate_in_worker) for _ in range(2)]
        barrier.wait()
        [future.result() for future in futures]

    assert len(clients) == 3
    assert len(clients[0].completions.calls) == 2
    assert sum(len(client.completions.calls) for client in clients) == 4
    assert all(kwargs["timeout"] == 12.5 for kwargs in client_kwargs)
    assert all(kwargs["max_retries"] == 0 for kwargs in client_kwargs)

    generator.close()
    generator.close()
    assert all(client.closed for client in clients)
    with pytest.raises(RuntimeError, match="is closed"):
        generator.generate(_user(), _videos())


def test_openrouter_retries_only_allowlisted_status_with_retry_after_and_backoff(
    monkeypatch,
):
    client = _FakeClient(
        [
            _StatusError(
                429,
                {"retry-after": "2", "x-openrouter-provider": "provider-a"},
            ),
            _StatusError(503, {"x-openrouter-provider": "provider-b"}),
            _success(),
        ]
    )
    sleeps = []
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.25)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=2,
        retry_backoff_base_seconds=1.0,
        retry_backoff_max_seconds=10.0,
    )

    assert generator.generate(_user(), _videos()) == '{"judgments": []}'
    assert len(client.completions.calls) == 3
    assert sleeps == [2.25, 2.25]


@pytest.mark.parametrize("status", [400, 401, 402, 403])
def test_openrouter_does_not_retry_non_retryable_client_errors(monkeypatch, status):
    client = _FakeClient(
        [_StatusError(status, {"x-openrouter-provider": "provider-a"})]
    )
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=3,
    )

    with pytest.raises(OpenRouterRequestError) as exc_info:
        generator.generate(_user(), _videos())

    assert len(client.completions.calls) == 1
    assert exc_info.value.status == status
    assert exc_info.value.provider == "provider-a"
    assert exc_info.value.attempts == 1
    assert "unsafe upstream detail" not in str(exc_info.value)


def test_openrouter_retry_exhaustion_is_structured(monkeypatch):
    client = _FakeClient([_StatusError(504), _StatusError(504), _StatusError(504)])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=2,
        retry_backoff_base_seconds=0.0,
        retry_backoff_max_seconds=0.0,
    )

    with pytest.raises(OpenRouterRequestError) as exc_info:
        generator.generate(_user(), _videos())

    assert exc_info.value.log_fields == {
        "status": 504,
        "error_type": "_StatusError",
        "provider": "unknown",
        "attempts": 3,
    }
    assert len(client.completions.calls) == 3


def test_openrouter_retries_api_timeout_with_separate_limit(monkeypatch):
    client = _FakeClient([_timeout_error(), _success()])
    sleeps = []
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", sleeps.append)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=1,
        timeout_max_retries=1,
        retry_backoff_base_seconds=0.0,
        retry_backoff_max_seconds=0.0,
    )

    assert generator.generate(_user(), _videos()) == '{"judgments": []}'
    assert len(client.completions.calls) == 2
    assert sleeps == [0.0]


def test_openrouter_timeout_retry_limit_is_independent_from_total_limit(monkeypatch):
    client = _FakeClient([_timeout_error(), _timeout_error(), _success()])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=3,
        timeout_max_retries=1,
        retry_backoff_base_seconds=0.0,
        retry_backoff_max_seconds=0.0,
    )

    with pytest.raises(OpenRouterRequestError) as exc_info:
        generator.generate(_user(), _videos())

    assert exc_info.value.status is None
    assert exc_info.value.error_type == "APITimeoutError"
    assert exc_info.value.attempts == 2
    assert len(client.completions.calls) == 2


def test_openrouter_retry_total_is_capped_across_timeout_and_http_errors(monkeypatch):
    client = _FakeClient(
        [_timeout_error(), _StatusError(503), _StatusError(503), _success()]
    )
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setattr(llm_module.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=2,
        timeout_max_retries=1,
        retry_backoff_base_seconds=0.0,
        retry_backoff_max_seconds=0.0,
    )

    with pytest.raises(OpenRouterRequestError) as exc_info:
        generator.generate(_user(), _videos())

    assert exc_info.value.status == 503
    assert exc_info.value.attempts == 3
    assert len(client.completions.calls) == 3


def test_openrouter_provider_preferences_are_optional_and_do_not_change_model(
    monkeypatch,
):
    for name in (
        "OPENROUTER_PROVIDER_SORT",
        "OPENROUTER_ALLOW_FALLBACKS",
        "OPENROUTER_REQUIRE_PARAMETERS",
    ):
        monkeypatch.delenv(name, raising=False)
    default_client = _FakeClient()
    configured_client = _FakeClient()
    clients = iter([default_client, configured_client])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: next(clients))

    default = OpenRouterActionLogGenerator(api_key="test-api-key")
    configured = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_sort="throughput",
        allow_fallbacks=False,
        require_parameters=True,
    )
    default.generate(_user(), _videos())
    configured.generate(_user(), _videos())

    default_request = default_client.completions.calls[0]
    configured_request = configured_client.completions.calls[0]
    assert "extra_body" not in default_request
    assert configured_request["extra_body"] == {
        "provider": {
            "sort": "throughput",
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }
    assert default_request["model"] == configured_request["model"]
    assert ":nitro" not in configured_request["model"]


def test_openrouter_default_routing_preserves_ambient_provider_preferences(
    monkeypatch,
):
    client = _FakeClient()
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "latency")
    monkeypatch.setenv("OPENROUTER_ALLOW_FALLBACKS", "false")
    monkeypatch.setenv("OPENROUTER_REQUIRE_PARAMETERS", "true")
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_routing_mode="default",
    )

    generator.generate(_user(), _videos())

    request = client.completions.calls[0]
    assert request["extra_body"] == {
        "provider": {
            "sort": "latency",
            "allow_fallbacks": False,
            "require_parameters": True,
        }
    }
    assert request["extra_headers"] == {"X-OpenRouter-Metadata": "enabled"}
    assert generator.fingerprint_config["provider_routing_mode"] == "default"
    assert generator.fingerprint_config["provider_preferences"] == {
        "sort": "latency",
        "allow_fallbacks": False,
        "require_parameters": True,
    }


def test_openrouter_auto_omits_provider_payload_despite_ambient_preferences(
    monkeypatch,
):
    client = _FakeClient()
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "not-a-valid-sort")
    monkeypatch.setenv("OPENROUTER_ALLOW_FALLBACKS", "not-a-boolean")
    monkeypatch.setenv("OPENROUTER_REQUIRE_PARAMETERS", "not-a-boolean")
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_routing_mode="auto",
        provider_sort="throughput",
        allow_fallbacks=False,
        require_parameters=True,
    )

    generator.generate(_user(), _videos())

    request = client.completions.calls[0]
    assert "extra_body" not in request
    assert request["extra_headers"] == {"X-OpenRouter-Metadata": "enabled"}
    assert generator.fingerprint_config["provider_preferences"] == {}


def test_openrouter_fixed_uses_only_normalized_slug_and_disables_fallbacks(
    monkeypatch,
):
    client = _FakeClient()
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    monkeypatch.setenv("OPENROUTER_PROVIDER_SORT", "not-a-valid-sort")
    monkeypatch.setenv("OPENROUTER_ALLOW_FALLBACKS", "not-a-boolean")
    monkeypatch.setenv("OPENROUTER_REQUIRE_PARAMETERS", "not-a-boolean")
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_routing_mode="fixed",
        provider_slug=" DeepInfra ",
        provider_sort="latency",
        allow_fallbacks=True,
        require_parameters=True,
    )

    generator.generate(_user(), _videos())

    request = client.completions.calls[0]
    assert request["extra_body"] == {
        "provider": {
            "only": ["deepinfra"],
            "allow_fallbacks": False,
        }
    }
    assert generator.provider_slug == "deepinfra"
    assert generator.fingerprint_config["provider_preferences"] == {
        "only": ["deepinfra"],
        "allow_fallbacks": False,
    }


@pytest.mark.parametrize(
    ("provider_routing_mode", "provider_slug", "message"),
    [
        ("invalid", None, "provider_routing_mode"),
        ("AUTO", None, "provider_routing_mode"),
        (" fixed ", "deepinfra", "provider_routing_mode"),
        ("default", "deepinfra", "only allowed"),
        ("auto", "deepinfra", "only allowed"),
        ("fixed", None, "is required"),
        ("fixed", "", "is required"),
        ("fixed", "deep infra", "valid OpenRouter provider slug"),
        ("fixed", "../deepinfra", "valid OpenRouter provider slug"),
        ("fixed", "deepinfra//turbo", "valid OpenRouter provider slug"),
    ],
)
def test_openrouter_provider_routing_rejects_invalid_mode_slug_combinations(
    provider_routing_mode,
    provider_slug,
    message,
):
    with pytest.raises(ValueError, match=message):
        OpenRouterActionLogGenerator(
            api_key="test-api-key",
            provider_routing_mode=provider_routing_mode,
            provider_slug=provider_slug,
        )


def test_openrouter_provider_mode_and_slug_change_fingerprint_without_model_change(
    monkeypatch,
):
    for name in (
        "OPENROUTER_PROVIDER_SORT",
        "OPENROUTER_ALLOW_FALLBACKS",
        "OPENROUTER_REQUIRE_PARAMETERS",
    ):
        monkeypatch.delenv(name, raising=False)
    default = OpenRouterActionLogGenerator(api_key="test-api-key")
    auto = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_routing_mode="auto",
    )
    fixed = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_routing_mode="fixed",
        provider_slug="deepinfra",
    )
    fixed_variant = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        provider_routing_mode="fixed",
        provider_slug="deepinfra/turbo",
    )

    assert default.model_name == auto.model_name == fixed.model_name
    assert default.fingerprint_config != auto.fingerprint_config
    assert auto.fingerprint_config != fixed.fingerprint_config
    assert fixed.fingerprint_config != fixed_variant.fingerprint_config


def test_openrouter_returns_invalid_json_without_retry(monkeypatch):
    client = _FakeClient([_success("{not valid json")])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=3,
    )

    assert generator.generate(_user(), _videos()) == "{not valid json"
    assert len(client.completions.calls) == 1


def test_openrouter_structured_logs_include_attempt_usage_without_sensitive_data(
    monkeypatch,
    caplog,
):
    response = SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content='{"judgments": []}'))],
        provider="provider-safe",
        usage=SimpleNamespace(
            prompt_tokens=120,
            completion_tokens=30,
            cost=0.001,
            completion_tokens_details=SimpleNamespace(reasoning_tokens=4),
        ),
    )
    client = _FakeClient([response])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.action_logs.llm_generator",
    ):
        with action_log_work_log_context(
            shard_index=2,
            work_sequence=7,
            detailed=True,
        ):
            generator.generate(_user(), _videos())

    events = [json.loads(record.message) for record in caplog.records]
    attempt = next(
        event for event in events if event["event"] == "openrouter_attempt_complete"
    )
    request = next(
        event for event in events if event["event"] == "openrouter_request_complete"
    )
    assert attempt["shard_index"] == request["shard_index"] == 2
    assert attempt["work_sequence"] == request["work_sequence"] == 7
    assert attempt["attempt"] == 1
    assert attempt["http_status"] == 200
    assert attempt["provider"] == "provider-safe"
    assert attempt["attempt_elapsed_ms"] >= 0
    assert request["request_elapsed_ms"] >= attempt["attempt_elapsed_ms"]
    assert request["retry_count"] == 0
    assert request["prompt_tokens"] == 120
    assert request["completion_tokens"] == 30
    assert request["reasoning_tokens"] == 4
    assert request["reported_cost"] == 0.001

    serialized = json.dumps(events, ensure_ascii=False)
    assert "test-api-key" not in serialized
    assert "vu_test" not in serialized
    assert "테스트 영상" not in serialized
    assert "judgments" not in serialized


def test_openrouter_official_router_metadata_is_safely_aggregated(
    monkeypatch,
    caplog,
):
    sensitive_summary = "raw prompt and response must stay hidden"
    sensitive_pipeline = "persona secret inside router metadata"
    sensitive_response = '{"secret_response_body": true}'
    response = _success(
        sensitive_response,
        provider="legacy-provider",
        model_extra={
            "openrouter_metadata": {
                "requested": "sensitive-requested-value",
                "strategy": "fallback",
                "summary": sensitive_summary,
                "attempt": 3,
                "endpoints": {
                    "total": 3,
                    "available": [
                        {"provider": "Provider A", "selected": False},
                        {"provider": "DeepInfra", "selected": True},
                    ],
                },
                "attempts": [
                    {"provider": "Provider A", "status": "429"},
                    {"provider": "Provider B", "status": 503},
                    {"provider": "DeepInfra", "status": 200},
                ],
                "pipeline": [
                    {
                        "type": "guardrail",
                        "data": {"raw": sensitive_pipeline},
                    }
                ],
            }
        },
    )
    client = _FakeClient([response])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.action_logs.llm_generator",
    ):
        with action_log_work_log_context(
            shard_index=1,
            work_sequence=9,
            detailed=True,
        ):
            assert generator.generate(_user(), _videos()) == sensitive_response

    events = [json.loads(record.message) for record in caplog.records]
    attempt = next(
        event for event in events if event["event"] == "openrouter_attempt_complete"
    )
    request = next(
        event for event in events if event["event"] == "openrouter_request_complete"
    )
    assert attempt["provider"] == "DeepInfra"
    assert request["provider"] == "DeepInfra"
    assert request["router_attempt_count"] == 3
    assert request["router_fallback_count"] == 2
    assert request["router_429_count"] == 1
    assert client.completions.calls[0]["extra_headers"] == {
        "X-OpenRouter-Metadata": "enabled"
    }

    serialized = json.dumps(events, ensure_ascii=False)
    assert sensitive_summary not in serialized
    assert sensitive_pipeline not in serialized
    assert sensitive_response not in serialized
    assert "sensitive-requested-value" not in serialized
    assert "openrouter_metadata" not in serialized


def test_openrouter_malformed_router_metadata_is_ignored_with_legacy_fallback(
    monkeypatch,
    caplog,
):
    sensitive_metadata = "malformed metadata secret"
    response = _success(
        model_extra={
            "provider": "legacy-provider",
            "openrouter_metadata": {
                "attempt": "not-an-integer",
                "endpoints": {
                    "available": [
                        None,
                        {
                            "provider": f"unsafe\n{sensitive_metadata}",
                            "selected": True,
                        },
                    ]
                },
                "attempts": {"unexpected": "object"},
                "summary": sensitive_metadata,
            },
        }
    )
    client = _FakeClient([response])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.action_logs.llm_generator",
    ):
        with action_log_work_log_context(
            shard_index=0,
            work_sequence=1,
            detailed=True,
        ):
            generator.generate(_user(), _videos())

    request = next(
        json.loads(record.message)
        for record in caplog.records
        if json.loads(record.message)["event"] == "openrouter_request_complete"
    )
    assert request["provider"] == "legacy-provider"
    assert "router_attempt_count" not in request
    assert "router_fallback_count" not in request
    assert "router_429_count" not in request
    assert sensitive_metadata not in json.dumps(request, ensure_ascii=False)


def test_openrouter_retry_log_separates_attempt_and_backoff(monkeypatch, caplog):
    client = _FakeClient(
        [
            _StatusError(429, {"x-openrouter-provider": "provider-a"}),
            _success(),
        ]
    )
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    event_order = []
    original_emit = llm_module.emit_action_log_event

    def _record_event(*args, **kwargs):
        event_order.append(args[2])
        return original_emit(*args, **kwargs)

    def _record_sleep(seconds):
        event_order.append("sleep")

    monkeypatch.setattr(llm_module, "emit_action_log_event", _record_event)
    monkeypatch.setattr(llm_module.time, "sleep", _record_sleep)
    monkeypatch.setattr(llm_module.random, "uniform", lambda start, end: 0.0)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=1,
        retry_backoff_base_seconds=1.0,
        retry_backoff_max_seconds=1.0,
    )

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.action_logs.llm_generator",
    ):
        with action_log_work_log_context(
            shard_index=0,
            work_sequence=0,
            detailed=True,
        ):
            generator.generate(_user(), _videos())

    events = [json.loads(record.message) for record in caplog.records]
    attempts = [
        event for event in events if event["event"] == "openrouter_attempt_complete"
    ]
    retry_scheduled = next(
        event for event in events if event["event"] == "openrouter_retry_scheduled"
    )
    request = next(
        event for event in events if event["event"] == "openrouter_request_complete"
    )
    assert len(attempts) == 2
    assert event_order.index("openrouter_retry_scheduled") < event_order.index("sleep")
    assert retry_scheduled["attempt"] == 1
    assert retry_scheduled["retry_count"] == 1
    assert retry_scheduled["backoff_seconds"] == 1.0
    assert retry_scheduled["http_status"] == 429
    assert retry_scheduled["provider"] == "provider-a"
    assert retry_scheduled["request_elapsed_ms"] >= 0
    assert attempts[0]["outcome"] == "retry"
    assert attempts[0]["http_status"] == 429
    assert attempts[0]["provider"] == "provider-a"
    assert attempts[0]["backoff_scheduled_ms"] == 1000.0
    assert attempts[0]["backoff_elapsed_ms"] >= 0
    assert attempts[1]["outcome"] == "success"
    assert request["retry_count"] == 1
    assert request["attempt"] == 2
    serialized = json.dumps(events, ensure_ascii=False)
    assert "test-api-key" not in serialized
    assert "vu_test" not in serialized
    assert "테스트 영상" not in serialized


def test_openrouter_success_detail_logs_are_suppressed_for_large_runs(
    monkeypatch,
    caplog,
):
    client = _FakeClient([_success()])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    with caplog.at_level(
        logging.INFO,
        logger="autoresearch.action_logs.llm_generator",
    ):
        with action_log_work_log_context(
            shard_index=0,
            work_sequence=0,
            detailed=False,
        ):
            generator.generate(_user(), _videos())

    assert caplog.records == []
