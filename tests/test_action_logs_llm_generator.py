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
    build_action_log_prompt,
)


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


def test_prompt_uses_complete_json_skeleton_without_ellipsis():
    videos = [
        {
            "video_id": f"video_{index}",
            "title": f"테스트 영상 {index}",
            "description": "설명",
            "tags": ["게임"],
        }
        for index in range(4)
    ]

    prompt = build_action_log_prompt(_user(), videos)

    assert "Prompt version: action_log_ctr_v3" in prompt
    assert 'required_indexes=[0,1,2,3]' in prompt
    assert "expected_count=4" in prompt
    assert '{"j":[[0,0.0,0.0],[1,0.0,0.0],[2,0.0,0.0],[3,0.0,0.0]]}' in prompt
    assert "..." not in prompt


def test_schema_retry_prompt_names_failure_without_reusing_raw_response(monkeypatch):
    client = _FakeClient([_success('{"j":[[0,0.2,0.3]]}')])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(api_key="test-api-key")

    result = generator.generate_schema_retry(
        _user(),
        _videos(),
        error_type="invalid_json",
    )

    prompt = client.completions.calls[0]["messages"][1]["content"]
    assert result == '{"j":[[0,0.2,0.3]]}'
    assert "이전 응답은 invalid_json 검증에 실패했다" in prompt
    assert '{"j":[[0,0.0,0.0]]}' in prompt


def _success(content: str = '{"judgments": []}'):
    return SimpleNamespace(
        choices=[SimpleNamespace(message=SimpleNamespace(content=content))]
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


def test_openrouter_returns_invalid_json_without_retry(monkeypatch):
    client = _FakeClient([_success("{not valid json")])
    monkeypatch.setattr("openai.OpenAI", lambda **kwargs: client)
    generator = OpenRouterActionLogGenerator(
        api_key="test-api-key",
        max_retries=3,
    )

    assert generator.generate(_user(), _videos()) == "{not valid json"
    assert len(client.completions.calls) == 1
