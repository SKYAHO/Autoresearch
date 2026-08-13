"""GitHub Pull Requests REST 클라이언트의 생성·중복·fail-closed 계약을 검증한다(#689).

[파이프라인] 완주한 실험의 exp 브랜치를 dev로 향하는 PR로 여는 REST 경계를 담당한다.
어떤 실험이 대상인지 판정하는 것과 installation token 발급은 검증하지 않는다.

[비책임] PR 대상 판정(`test_experiment_pull_request.py`), token 발급(`github_app.py`),
App 권한 부여(`SKYAHO/Autoresearch-infra#629`)는 다루지 않는다.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
import sys

import httpx
import pytest

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from applications.experiment_platform.shared.github_pull_requests import (  # noqa: E402
    GitHubPullRequestError,
    GitHubPullRequests,
)


class RecordingTransport(httpx.AsyncBaseTransport):
    """요청을 기록하고 준비된 응답을 순서대로 반환한다."""

    def __init__(self, *responses: httpx.Response) -> None:
        self._responses = list(responses)
        self.requests: list[httpx.Request] = []

    async def handle_async_request(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        return self._responses.pop(0)


def _created(number: int) -> httpx.Response:
    return httpx.Response(201, json={"number": number, "html_url": "https://x/1"})


def test_create_returns_the_pull_request_number() -> None:
    transport = RecordingTransport(_created(7))
    client = GitHubPullRequests(transport=transport)

    number = asyncio.run(
        client.create(
            "SKYAHO/Autoresearch",
            head="exp/689-demo",
            base="dev",
            title="[AR] #689 데모",
            body="## 결론",
            token="t",
        )
    )

    assert number == 7


def test_create_sends_the_branch_coordinates_github_expects() -> None:
    """`head`/`base`가 한 글자만 틀려도 422가 나거나 엉뚱한 브랜치가 열린다."""
    transport = RecordingTransport(_created(7))
    client = GitHubPullRequests(transport=transport)

    asyncio.run(
        client.create(
            "SKYAHO/Autoresearch",
            head="exp/689-demo",
            base="dev",
            title="[AR] #689 데모",
            body="## 결론",
            token="t",
        )
    )

    request = transport.requests[0]
    assert request.method == "POST"
    assert request.url.path == "/repos/SKYAHO/Autoresearch/pulls"
    payload = json.loads(request.content)
    assert payload["head"] == "exp/689-demo"
    assert payload["base"] == "dev"
    assert payload["title"] == "[AR] #689 데모"
    assert payload["body"] == "## 결론"


def test_create_does_not_leak_the_token_into_errors() -> None:
    """실패 사유에 자격 증명이 섞이면 로그·워크벤치로 그대로 흘러간다."""
    transport = RecordingTransport(httpx.Response(500, text="boom"))
    client = GitHubPullRequests(transport=transport)

    with pytest.raises(GitHubPullRequestError) as caught:
        asyncio.run(
            client.create(
                "SKYAHO/Autoresearch",
                head="exp/689-demo",
                base="dev",
                title="t",
                body="b",
                token="super-secret-token",
            )
        )

    message = str(caught.value)
    assert "super-secret-token" not in message
    assert "boom" not in message
    assert caught.value.status_code == 500


def test_missing_permission_is_its_own_reason() -> None:
    """403은 App에 `Pull requests: write`가 없다는 신호다.

    일시 장애와 뭉치면 권한을 안 준 것을 모른 채 매 주기 재시도만 하게 된다.
    """
    transport = RecordingTransport(httpx.Response(403, json={}))
    client = GitHubPullRequests(transport=transport)

    with pytest.raises(GitHubPullRequestError) as caught:
        asyncio.run(
            client.create(
                "SKYAHO/Autoresearch",
                head="h",
                base="dev",
                title="t",
                body="b",
                token="t",
            )
        )

    assert caught.value.reason == "pull_request_forbidden"


def test_existing_pull_request_is_reported_as_duplicate() -> None:
    """같은 head→base에 PR이 이미 있으면 GitHub이 422로 거부한다.

    이건 오류가 아니라 "먼저 만들고 기록 전에 죽은" 흔적이다. 호출자가 기존 번호를
    조회해 기록만 채울 수 있도록 별도 사유로 구분한다.
    """
    transport = RecordingTransport(
        httpx.Response(
            422,
            json={"errors": [{"message": "A pull request already exists for x."}]},
        )
    )
    client = GitHubPullRequests(transport=transport)

    with pytest.raises(GitHubPullRequestError) as caught:
        asyncio.run(
            client.create(
                "SKYAHO/Autoresearch",
                head="h",
                base="dev",
                title="t",
                body="b",
                token="t",
            )
        )

    assert caught.value.reason == "pull_request_exists"


def test_no_commits_between_is_not_a_duplicate() -> None:
    """같은 422여도 "차이가 없다"는 다른 상황이다 — 기존 PR을 찾아봐야 소용없다."""
    transport = RecordingTransport(
        httpx.Response(
            422,
            json={"errors": [{"message": "No commits between dev and exp/689-demo"}]},
        )
    )
    client = GitHubPullRequests(transport=transport)

    with pytest.raises(GitHubPullRequestError) as caught:
        asyncio.run(
            client.create(
                "SKYAHO/Autoresearch",
                head="h",
                base="dev",
                title="t",
                body="b",
                token="t",
            )
        )

    assert caught.value.reason == "pull_request_no_commits"


def test_find_open_returns_the_number_for_an_existing_branch() -> None:
    transport = RecordingTransport(httpx.Response(200, json=[{"number": 11}]))
    client = GitHubPullRequests(transport=transport)

    number = asyncio.run(
        client.find_open(
            "SKYAHO/Autoresearch", head="exp/689-demo", base="dev", token="t"
        )
    )

    assert number == 11
    request = transport.requests[0]
    assert request.url.params["head"] == "SKYAHO:exp/689-demo"
    assert request.url.params["state"] == "open"


def test_find_open_returns_none_when_no_pull_request_is_open() -> None:
    transport = RecordingTransport(httpx.Response(200, json=[]))
    client = GitHubPullRequests(transport=transport)

    number = asyncio.run(
        client.find_open(
            "SKYAHO/Autoresearch", head="exp/689-demo", base="dev", token="t"
        )
    )

    assert number is None


def test_malformed_response_is_rejected_rather_than_guessed() -> None:
    """번호를 못 읽으면 기록할 수 없다 — 조용히 0이나 None으로 넘기지 않는다."""
    transport = RecordingTransport(httpx.Response(201, json={"html_url": "https://x"}))
    client = GitHubPullRequests(transport=transport)

    with pytest.raises(GitHubPullRequestError) as caught:
        asyncio.run(
            client.create(
                "SKYAHO/Autoresearch",
                head="h",
                base="dev",
                title="t",
                body="b",
                token="t",
            )
        )

    assert caught.value.reason == "invalid_response"


def test_find_open_filters_by_base_too() -> None:
    """"이미 존재" 422는 head→base 쌍 판정이다.

    head만으로 찾으면 같은 브랜치에서 `main`으로 따로 열어 둔 PR의 번호를 이 실험의
    기록으로 남길 수 있다 — 예외가 아니라 잘못된 성공으로 나타난다.
    """
    transport = RecordingTransport(httpx.Response(200, json=[{"number": 11}]))
    client = GitHubPullRequests(transport=transport)

    asyncio.run(
        client.find_open(
            "SKYAHO/Autoresearch", head="exp/689-demo", base="dev", token="t"
        )
    )

    params = transport.requests[0].url.params
    assert params["head"] == "SKYAHO:exp/689-demo"
    assert params["base"] == "dev"


def test_find_open_refuses_to_guess_between_several_pull_requests() -> None:
    """같은 좌표에 둘 이상이면 어느 것이 이 실험의 것인지 알 수 없다."""
    transport = RecordingTransport(
        httpx.Response(200, json=[{"number": 11}, {"number": 12}])
    )
    client = GitHubPullRequests(transport=transport)

    with pytest.raises(GitHubPullRequestError) as caught:
        asyncio.run(
            client.find_open(
                "SKYAHO/Autoresearch", head="h", base="dev", token="t"
            )
        )

    assert caught.value.reason == "invalid_response"


def test_too_long_body_has_its_own_reason() -> None:
    """본문 상한 초과는 재시도해도 낫지 않는다 — 일반 실패와 뭉치면 영원히 되풀이한다."""
    transport = RecordingTransport(
        httpx.Response(
            422,
            json={
                "errors": [
                    {"message": "body is too long (maximum is 65536 characters)"}
                ]
            },
        )
    )
    client = GitHubPullRequests(transport=transport)

    with pytest.raises(GitHubPullRequestError) as caught:
        asyncio.run(
            client.create(
                "SKYAHO/Autoresearch",
                head="h",
                base="dev",
                title="t",
                body="b",
                token="t",
            )
        )

    assert caught.value.reason == "pull_request_body_too_long"
