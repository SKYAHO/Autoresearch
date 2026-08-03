"""렌더한 Auto Research 이슈 본문을 자가 검증한 뒤 GitHub에 발행하는 CLI입니다.

[파이프라인] 자율 실험 진입점에서 본문 렌더와 이슈별 실험 브랜치 생성 워크플로 사이
— 발행 전 계약 게이트와 폭주 방지, 실제 이슈 생성을 담당합니다.

[기능] 초안 파일을 읽어 `tools/auto_research_issue_body.py`로 본문을 렌더하고,
그 출력을 그대로 파싱 정본 `parse_issue_input()`에 넣어 placeholder 이슈 번호로 자가
검증합니다. dry-run이 기본값이고, 1회 실행당 발행 상한과 `연구 가설`·
`변경할 피처 · 모델` 기반 재발행 차단 키(`hypothesis_dedupe_key()`)를 적용하며,
발행 시 `auto-research`·`experiment` label을 함께 부여합니다. 발행 후에는 실제 이슈
번호로 `issue_branch`만 다시 계산합니다.

[비책임] 필드 값의 의미 검증과 식별자 계산은 `tools/auto_research_issue_branch.py`,
본문 조립은 `tools/auto_research_issue_body.py`, exp 브랜치 생성과 marker 코멘트는
`.github/workflows/auto-research-issue-branch.yml`이 소유합니다.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
import json
import os
from pathlib import Path
import re
from typing import Callable, Mapping, Sequence
import urllib.error
import urllib.parse
import urllib.request

from tools.auto_research_issue_body import render_issue_body
from tools.auto_research_issue_branch import (
    IssueInput,
    _identifier,
    branch_name_for,
    parse_issue_input,
)


#: 발행 전에는 이슈 번호가 없다. `parse_issue_input()`이 `branch_name_for()`를 호출하고
#: `issue_number <= 0`이면 예외를 던지므로 사전 검증은 이 placeholder로 수행한다.
#: `criteria_id`/`reproducibility_id`는 이슈 번호에 의존하지 않아 재계산하지 않는다.
PLACEHOLDER_ISSUE_NUMBER = 1
#: 워크플로 job은 두 label을 동시에 가질 때만 실행된다
#: (`.github/workflows/auto-research-issue-branch.yml`).
REQUIRED_LABELS: tuple[str, ...] = ("auto-research", "experiment")
DEFAULT_MAX_ISSUES = 1
GITHUB_API_ROOT = "https://api.github.com"
ISSUE_PAGE_SIZE = 100
MAX_ISSUE_PAGES = 10
REQUEST_TIMEOUT_SEC = 30
TOKEN_ENVIRONMENT_VARIABLE = "AUTO_RESEARCH_ISSUE_TOKEN"

_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9._-]+/[A-Za-z0-9._-]+$")
_DIGEST_SLUG_PATTERN = re.compile(r"^exp/\d+-issue-[0-9a-f]{12}$")
_DRAFT_KEYS = frozenset({"title", "fields", "allowed_scope"})

#: (method, path, payload) → 파싱된 JSON. 기본 구현은 stdlib urllib이며 테스트는
#: 이 seam에 fake를 주입한다. 토큰은 기본 구현의 closure에만 존재한다.
RequestFn = Callable[[str, str, Mapping[str, object] | None], object]


def hypothesis_dedupe_key(issue_input: IssueInput) -> str:
    """같은 가설·같은 변경의 재발행을 막는 차단 키를 계산합니다.

    `criteria_id`(주 지표 6필드)와 `reproducibility_id`(dataset/seed/split/config
    6필드)에는 `연구 가설`도 `변경할 피처 · 모델`도 들어가지 않는다. 두 식별자의
    조합을 차단 키로 쓰면, 같은 스냅샷·시드·지표 기준 위에서 피처만 바꿔 반복하는
    정상 사용 패턴이 전부 중복으로 거부되고, 반대로 가설을 그대로 둔 채 시드 하나만
    바꾸면 차단을 우회한다. 그래서 차단 키는 가설과 변경 내용만으로 따로 계산한다.

    이 해시는 **발행 도구의 것이지 파싱 계약의 것이 아니다.** marker 봉인 계약과
    무관해야 하므로 `IssueInput`이나 `parse_issue_input()`의 반환값에 넣지 않는다.
    canonical JSON + SHA-256 방식은 `_identifier()`를 그대로 재사용한다.

    Args:
        issue_input: 발행 전 자가 검증을 통과한 이슈 계약입니다.

    Returns:
        `연구 가설`과 `변경할 피처 · 모델`만 묶은 64자 SHA-256 식별자입니다.
    """
    # 내용 해시이므로 같은 가설을 다르게 고쳐 쓰면 키가 달라진다. 문구를 정규화해
    # 동일 판정을 넓히지 않는다 — 넓히면 서로 다른 실험을 잘못 차단한다.
    return _identifier({"hypothesis": issue_input.hypothesis, "change": issue_input.change})


@dataclass(frozen=True)
class PreparedIssue:
    """발행 전 자가 검증을 통과한 이슈 초안입니다."""

    title: str
    body: str
    criteria_id: str
    reproducibility_id: str
    dedupe_key: str
    readable_branch_slug: bool


@dataclass(frozen=True)
class PublishedIssue:
    """실제로 발행된 이슈와 발행 후 확정된 브랜치 이름입니다."""

    number: int
    url: str
    title: str
    issue_branch: str


@dataclass(frozen=True)
class OpenIssueSurvey:
    """열린 Auto Research 이슈에서 읽어낸 차단 키 집합입니다."""

    dedupe_keys: frozenset[str]
    unparsed_issue_numbers: tuple[int, ...]


@dataclass(frozen=True)
class PublishOutcome:
    """실제 발행 1회의 결과와 차단 키 확인에서 제외한 이슈입니다."""

    published: tuple[PublishedIssue, ...]
    unparsed_issue_numbers: tuple[int, ...]


def load_drafts(drafts_file: Path) -> tuple[dict[str, object], ...]:
    """초안 JSON 배열을 읽어 draft object tuple로 반환합니다.

    Args:
        drafts_file: draft object의 JSON 배열 파일 경로입니다.

    Returns:
        검증되지 않은 draft object tuple입니다.

    Raises:
        ValueError: JSON이 아니거나 배열이 아니거나 알 수 없는 키가 있을 때 발생합니다.
    """
    try:
        parsed = json.loads(drafts_file.read_text(encoding="utf-8"))
    except json.JSONDecodeError as error:
        raise ValueError(f"초안 파일이 올바른 JSON이 아닙니다: {drafts_file}") from error
    if not isinstance(parsed, list) or not all(isinstance(draft, dict) for draft in parsed):
        raise ValueError("초안 파일은 draft object의 JSON 배열이어야 합니다")

    drafts: list[dict[str, object]] = []
    for index, draft in enumerate(parsed):
        unknown_keys = sorted(set(draft) - _DRAFT_KEYS)
        if unknown_keys:
            raise ValueError(f"초안 {index}에 알 수 없는 키가 있습니다: " + ", ".join(unknown_keys))
        drafts.append(draft)
    return tuple(drafts)


def prepare_drafts(
    drafts: Sequence[Mapping[str, object]],
    max_issues: int = DEFAULT_MAX_ISSUES,
) -> tuple[PreparedIssue, ...]:
    """모든 초안을 렌더하고 파싱 정본으로 자가 검증합니다.

    하나라도 실패하면 아무것도 발행하지 않도록 여기서 즉시 중단합니다.

    Args:
        drafts: `load_drafts()`가 반환한 draft object입니다.
        max_issues: 1회 실행당 발행 상한입니다.

    Returns:
        검증을 통과한 이슈 초안입니다.

    Raises:
        ValueError: 상한 초과, 제목 규칙 위반, 렌더·파싱 실패, 배치 내 중복 가설이
            있을 때 발생합니다.
    """
    if max_issues < 1:
        raise ValueError("max_issues는 1 이상이어야 합니다")
    if not drafts:
        raise ValueError("초안이 하나도 없습니다")
    if len(drafts) > max_issues:
        raise ValueError(f"초안 {len(drafts)}건이 1회 실행 상한 {max_issues}건을 넘습니다")

    prepared: list[PreparedIssue] = []
    seen_keys: set[str] = set()
    for index, draft in enumerate(drafts):
        try:
            prepared_issue = _prepare_draft(draft)
        except ValueError as error:
            raise ValueError(f"초안 {index} 검증 실패: {error}") from error
        if prepared_issue.dedupe_key in seen_keys:
            raise ValueError(f"초안 {index}가 같은 배치의 다른 초안과 동일한 가설입니다")
        seen_keys.add(prepared_issue.dedupe_key)
        prepared.append(prepared_issue)
    return tuple(prepared)


def _prepare_draft(draft: Mapping[str, object]) -> PreparedIssue:
    """초안 하나를 렌더하고 placeholder 이슈 번호로 자가 검증합니다."""
    title = draft.get("title")
    if not isinstance(title, str) or not title.strip():
        raise ValueError("title은 비어 있지 않은 문자열이어야 합니다")
    title = title.strip()
    # `[AR] ` prefix는 강제하지 않는다. 저장소 계약 어디에도 요구가 없다 —
    # 워크플로는 제목을 검사하지 않고, `branch_name_for()`는 prefix가 있으면
    # slug 생성 전에 제거할 뿐 없다고 실패하지 않는다. Issue Form이 미리 채워
    # 주는 관례일 뿐이므로 발행 게이트로 승격시키지 않는다.

    fields = draft.get("fields")
    if not isinstance(fields, dict):
        raise ValueError("fields는 object여야 합니다")
    allowed_scope = draft.get("allowed_scope", [])
    if not isinstance(allowed_scope, list) or not all(
        isinstance(scope, str) for scope in allowed_scope
    ):
        raise ValueError("allowed_scope는 문자열 배열이어야 합니다")

    body = render_issue_body(fields, allowed_scope)
    issue_input = _self_validated(title, body)
    return PreparedIssue(
        title=title,
        body=body,
        criteria_id=issue_input.criteria_id,
        reproducibility_id=issue_input.reproducibility_id,
        dedupe_key=hypothesis_dedupe_key(issue_input),
        readable_branch_slug=_DIGEST_SLUG_PATTERN.fullmatch(issue_input.issue_branch) is None,
    )


def _self_validated(title: str, body: str) -> IssueInput:
    """렌더 결과를 파싱 정본에 그대로 넣어 발행 전 게이트를 통과시킵니다."""
    return parse_issue_input(PLACEHOLDER_ISSUE_NUMBER, title, body)


def survey_open_issues(repository: str, request: RequestFn) -> OpenIssueSurvey:
    """열린 Auto Research 이슈에서 이미 사용 중인 차단 키를 모읍니다.

    Args:
        repository: `owner/name` 형식의 대상 저장소입니다.
        request: GitHub JSON 요청 seam입니다.

    Returns:
        기존 차단 키와, 본문이 계약을 만족하지 않아 건너뛴 이슈 번호입니다.
    """
    dedupe_keys: set[str] = set()
    unparsed: list[int] = []
    for page in range(1, MAX_ISSUE_PAGES + 1):
        query = urllib.parse.urlencode(
            {
                "labels": ",".join(REQUIRED_LABELS),
                "state": "open",
                "per_page": ISSUE_PAGE_SIZE,
                "page": page,
            }
        )
        payload = request("GET", f"/repos/{repository}/issues?{query}", None)
        if not isinstance(payload, list):
            raise RuntimeError("GitHub 이슈 목록 응답이 배열이 아닙니다")
        if not payload:
            break
        for issue in payload:
            if not isinstance(issue, dict) or "pull_request" in issue:
                continue
            number = issue.get("number")
            title = issue.get("title")
            body = issue.get("body")
            if not isinstance(number, int) or not isinstance(title, str) or not isinstance(body, str):
                continue
            try:
                parsed = parse_issue_input(number, title, body)
            except ValueError:
                unparsed.append(number)
                continue
            dedupe_keys.add(hypothesis_dedupe_key(parsed))
        if len(payload) < ISSUE_PAGE_SIZE:
            break
    return OpenIssueSurvey(
        dedupe_keys=frozenset(dedupe_keys),
        unparsed_issue_numbers=tuple(unparsed),
    )


def publish_issues(
    prepared: Sequence[PreparedIssue],
    repository: str,
    request: RequestFn,
) -> PublishOutcome:
    """차단 키를 확인한 뒤 이슈를 생성하고 발행 후 브랜치 이름을 확정합니다.

    Args:
        prepared: 자가 검증을 통과한 초안입니다.
        repository: `owner/name` 형식의 대상 저장소입니다.
        request: GitHub JSON 요청 seam입니다.

    Returns:
        발행된 이슈(이슈 번호로 다시 계산한 `issue_branch` 포함)와 차단 키 확인에서
        제외한 이슈 번호입니다.

    Raises:
        ValueError: 같은 가설의 열린 이슈가 이미 있을 때 발생합니다.
        RuntimeError: GitHub 응답이 계약을 만족하지 않을 때 발생합니다.
    """
    _validate_repository(repository)
    survey = survey_open_issues(repository, request)
    blocked = [issue for issue in prepared if issue.dedupe_key in survey.dedupe_keys]
    if blocked:
        raise ValueError(
            "같은 연구 가설·변경 내용의 열린 이슈가 이미 있습니다: "
            + ", ".join(issue.title for issue in blocked)
        )

    published: list[PublishedIssue] = []
    for issue in prepared:
        payload = request(
            "POST",
            f"/repos/{repository}/issues",
            {
                "title": issue.title,
                "body": issue.body,
                "labels": list(REQUIRED_LABELS),
            },
        )
        if not isinstance(payload, dict):
            raise RuntimeError("GitHub 이슈 생성 응답이 object가 아닙니다")
        number = payload.get("number")
        url = payload.get("html_url")
        if not isinstance(number, int) or not isinstance(url, str):
            raise RuntimeError("GitHub 이슈 생성 응답에 number 또는 html_url이 없습니다")
        published.append(
            PublishedIssue(
                number=number,
                url=url,
                title=issue.title,
                issue_branch=branch_name_for(number, issue.title),
            )
        )
    return PublishOutcome(
        published=tuple(published),
        unparsed_issue_numbers=survey.unparsed_issue_numbers,
    )


def _validate_repository(repository: str) -> None:
    """대상 저장소가 `owner/name` 형식인지 확인합니다."""
    if _REPOSITORY_PATTERN.fullmatch(repository) is None:
        raise ValueError("repository는 owner/name 형식이어야 합니다")


def github_request(token: str) -> RequestFn:
    """`issues: write` 토큰을 closure에 담은 stdlib 기반 요청 함수를 만듭니다.

    Args:
        token: `issues: write` 권한만 가진 GitHub 토큰입니다.

    Returns:
        (method, path, payload)를 받아 파싱된 JSON을 반환하는 함수입니다.
    """

    def request(method: str, path: str, payload: Mapping[str, object] | None) -> object:
        body = json.dumps(payload).encode("utf-8") if payload is not None else None
        http_request = urllib.request.Request(
            f"{GITHUB_API_ROOT}{path}",
            data=body,
            method=method,
            headers={
                "Accept": "application/vnd.github+json",
                "Authorization": f"Bearer {token}",
                "Content-Type": "application/json",
                "User-Agent": "autoresearch-auto-research-issue-publish",
                "X-GitHub-Api-Version": "2022-11-28",
            },
        )
        try:
            with urllib.request.urlopen(http_request, timeout=REQUEST_TIMEOUT_SEC) as response:
                return json.loads(response.read().decode("utf-8"))
        except urllib.error.HTTPError as error:
            # 토큰과 응답 본문을 남기지 않는다. 작업 이름과 정제된 경로만 보고한다.
            raise RuntimeError(
                f"GitHub 요청 실패: {method} {_sanitized_path(path)} → HTTP {error.code}"
            ) from None
        except urllib.error.URLError:
            raise RuntimeError(
                f"GitHub 요청 실패: {method} {_sanitized_path(path)} → 연결 오류"
            ) from None

    return request


def _sanitized_path(path: str) -> str:
    """보고에 쓸 수 있도록 query string을 제거한 경로를 반환합니다."""
    return path.split("?", 1)[0]


def _resolved_token() -> str:
    """환경 변수에서 토큰을 읽습니다. 값은 어디에도 출력하지 않습니다."""
    token = os.environ.get(TOKEN_ENVIRONMENT_VARIABLE, "").strip()
    if not token:
        raise ValueError(
            f"{TOKEN_ENVIRONMENT_VARIABLE}가 비어 있습니다 (issues: write 권한만 필요합니다)"
        )
    return token


def _dry_run_report(prepared: Sequence[PreparedIssue]) -> str:
    """발행하지 않고 사람이 검토할 수 있는 보고를 만듭니다."""
    lines = [f"dry-run: 검증을 통과한 초안 {len(prepared)}건 (발행하지 않았습니다)"]
    for issue in prepared:
        lines.extend(
            (
                "",
                f"title={issue.title}",
                f"labels={','.join(REQUIRED_LABELS)}",
                f"criteria_id={issue.criteria_id}",
                f"reproducibility_id={issue.reproducibility_id}",
                f"hypothesis_dedupe_key={issue.dedupe_key}",
            )
        )
        if not issue.readable_branch_slug:
            lines.append(
                "warning: 제목에 ASCII 영소문자·숫자가 없어 브랜치 slug가 digest로 대체됩니다"
            )
        lines.extend(("body:", issue.body.rstrip("\n")))
    return "\n".join(lines)


def _publish_report(outcome: PublishOutcome) -> str:
    """발행 결과와 건너뛴 이슈를 보고합니다."""
    lines = [f"발행 완료: {len(outcome.published)}건"]
    for issue in outcome.published:
        lines.extend(
            (
                f"issue_number={issue.number}",
                f"issue_url={issue.url}",
                f"issue_branch={issue.issue_branch}",
            )
        )
    if outcome.unparsed_issue_numbers:
        lines.append(
            "note: 본문이 계약을 만족하지 않아 차단 키 확인에서 제외한 열린 이슈 — "
            + ", ".join(f"#{number}" for number in outcome.unparsed_issue_numbers)
        )
    return "\n".join(lines)


def _parse_arguments(argv: Sequence[str] | None) -> argparse.Namespace:
    """CLI에서 초안 파일과 발행 대상·상한을 읽습니다."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--drafts-file", required=True, type=Path)
    parser.add_argument("--repository", help="owner/name. --publish에 필요합니다.")
    parser.add_argument(
        "--publish",
        action="store_true",
        help="실제로 발행합니다. 기본값은 dry-run입니다.",
    )
    parser.add_argument("--max-issues", type=int, default=DEFAULT_MAX_ISSUES)
    return parser.parse_args(argv)


def main(argv: Sequence[str] | None = None) -> int:
    """초안을 검증하고 dry-run 보고 또는 실제 발행 결과를 출력합니다."""
    arguments = _parse_arguments(argv)
    try:
        drafts = load_drafts(arguments.drafts_file)
        prepared = prepare_drafts(drafts, max_issues=arguments.max_issues)
    except (OSError, ValueError) as error:
        print(f"발행 중단: {error}")
        return 1

    if not arguments.publish:
        print(_dry_run_report(prepared))
        return 0

    try:
        if not arguments.repository:
            raise ValueError("--publish에는 --repository가 필요합니다")
        _validate_repository(arguments.repository)
        request = github_request(_resolved_token())
        outcome = publish_issues(prepared, arguments.repository, request)
    except (RuntimeError, ValueError) as error:
        print(f"발행 중단: {error}")
        return 1
    print(_publish_report(outcome))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
