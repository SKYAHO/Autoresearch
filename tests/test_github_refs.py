"""GitHub Git refs REST 클라이언트의 조회·생성·fail-closed 계약을 검증한다.

전체 파이프라인에서 봉인된 기준 SHA를 읽고 그 SHA에 exp ref를 만드는 REST 경계를
담당한다. installation token 발급, branch 멱등 판단과 Git push는 검증하지 않는다.
"""

from __future__ import annotations

import asyncio
import json

import httpx
import pytest

from agent_orchestration.github_refs import GitHubRefError, GitHubRefs


class RecordingTransport(httpx.AsyncBaseTransport):
    """한 요청을 기록하고 준비된 GitHub 응답을 반환한다."""

    def __init__(self, response: httpx.Response) -> None:
        self.response = response
        self.request: httpx.Request | None = None

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.request = request
        return self.response


def test_get_sha_returns_none_only_for_missing_ref() -> None:
    transport = RecordingTransport(httpx.Response(404, text="not found"))

    result = asyncio.run(
        GitHubRefs(transport=transport).get_sha(
            "SKYAHO/Autoresearch", "heads/dev", "secret-token"
        )
    )

    assert result is None
    assert transport.request is not None
    assert transport.request.url.path == "/repos/SKYAHO/Autoresearch/git/ref/heads/dev"


def test_get_sha_returns_a_valid_lowercase_commit_sha() -> None:
    transport = RecordingTransport(
        httpx.Response(200, json={"object": {"sha": "a" * 40}})
    )

    result = asyncio.run(
        GitHubRefs(transport=transport).get_sha(
            "SKYAHO/Autoresearch", "heads/dev", "secret-token"
        )
    )

    assert result == "a" * 40


def test_get_sha_rejects_an_untrusted_response_shape() -> None:
    transport = RecordingTransport(
        httpx.Response(200, json={"object": {"sha": "ABC-not-a-commit"}})
    )

    with pytest.raises(GitHubRefError, match="invalid_response"):
        asyncio.run(
            GitHubRefs(transport=transport).get_sha(
                "SKYAHO/Autoresearch", "heads/dev", "secret-token"
            )
        )


def test_create_sends_the_full_ref_and_frozen_sha() -> None:
    transport = RecordingTransport(
        httpx.Response(201, json={"object": {"sha": "a" * 40}})
    )

    result = asyncio.run(
        GitHubRefs(transport=transport).create(
            "SKYAHO/Autoresearch",
            "heads/exp-546",
            "a" * 40,
            "secret-token",
        )
    )

    assert result == "a" * 40
    assert transport.request is not None
    assert transport.request.url.path == "/repos/SKYAHO/Autoresearch/git/refs"
    assert json.loads(transport.request.content) == {
        "ref": "refs/heads/exp-546",
        "sha": "a" * 40,
    }


def test_non_404_error_is_typed_and_sanitized() -> None:
    transport = RecordingTransport(
        httpx.Response(403, text="private-response-body secret-token")
    )

    with pytest.raises(GitHubRefError) as captured:
        asyncio.run(
            GitHubRefs(transport=transport).get_sha(
                "SKYAHO/Autoresearch", "heads/dev", "secret-token"
            )
        )

    assert captured.value.status_code == 403
    message = str(captured.value)
    assert "private-response-body" not in message
    assert "secret-token" not in message
    assert "Authorization" not in message
