"""PASSED 실험의 PR 생성 판정·조립·멱등 계약을 검증한다(#689).

[파이프라인] 실험이 완주해 `PASSED`가 된 뒤부터 `exp` 브랜치가 `dev`로 향하는 Pull
Request가 열리기까지의 구간을 담당한다. 머지와 `PROMOTED` 전이는 사람이 한다.

[기능] 어떤 실험이 대상인지 판정하고, 제목·본문을 조립하며, 같은 실험을 두 번
처리해도 PR이 하나만 생기게 하는 것을 검증한다.

[비책임] 실험 실행과 상태 전이(`test_experiment_launcher.py`), 리포트 생성
(executor), App 권한 부여(`SKYAHO/Autoresearch-infra`)는 다루지 않는다.
"""

from __future__ import annotations

from pathlib import Path
import sys
import uuid

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from applications.experiment_platform.launcher.pull_request import (  # noqa: E402
    PULL_REQUEST_METADATA_KEY,
    SkipReason,
    build_pull_request_body,
    build_pull_request_title,
    pull_request_plan,
)


class _Experiment:
    """`pull_request_plan`이 읽는 최소 Experiment 속성."""

    def __init__(
        self,
        *,
        issue_branch: str | None = "exp/689-demo",
        issue_number: int | None = 689,
        issue_title: str | None = "learning_rate를 0.03으로 낮춘다",
        candidate_sha: str | None = "a" * 40,
        report_markdown: str | None = "## 결론\n\n개선이 없었다.",
    ) -> None:
        self.id = uuid.UUID("6ec09890-a4a8-4c69-9760-c01349351505")
        self.issue_branch = issue_branch
        self.issue_number = issue_number
        self.issue_title = issue_title
        self.candidate_sha = candidate_sha
        self.report_markdown = report_markdown


def test_plan_targets_a_completed_experiment() -> None:
    """준비가 끝난 실험은 PR 대상이다."""
    plan = pull_request_plan(_Experiment(), recorded_number=None)

    assert plan.skip_reason is None
    assert plan.head == "exp/689-demo"
    assert plan.base == "dev"


def test_plan_does_not_filter_on_metrics() -> None:
    """지표가 나빠도 PR을 만든다.

    `PASSED`는 "가설이 맞았다"가 아니라 "완주했다"이다(정본 계약 §결정 1·6). 여기서
    결과로 거르면 승격 관문에서 제거한 통계 게이트를 다른 이름으로 되살리는 것이다.
    """
    experiment = _Experiment(report_markdown="## 결론\n\n**개선 없음.** 기각한다.")

    plan = pull_request_plan(experiment, recorded_number=None)

    assert plan.skip_reason is None


def test_plan_skips_when_a_pull_request_is_already_recorded() -> None:
    """이미 만든 실험은 건너뛴다 — GitHub에 묻지 않고 기록으로 판정한다."""
    plan = pull_request_plan(_Experiment(), recorded_number=42)

    assert plan.skip_reason is SkipReason.ALREADY_RECORDED


def test_plan_waits_until_the_report_exists() -> None:
    """본문 없는 PR보다 기다리는 편이 낫다. 다음 주기에 다시 본다."""
    plan = pull_request_plan(_Experiment(report_markdown=None), recorded_number=None)

    assert plan.skip_reason is SkipReason.REPORT_MISSING


def test_plan_skips_permanently_when_nothing_was_committed() -> None:
    """`candidate_sha`가 없으면 올릴 변경이 없다.

    다시 볼 필요가 없는 종류다 — 재시도 대상으로 두면 그 실험이 매 주기 돌아온다.
    """
    plan = pull_request_plan(_Experiment(candidate_sha=None), recorded_number=None)

    assert plan.skip_reason is SkipReason.NO_CHANGES
    assert plan.skip_reason.permanent is True


def test_report_missing_is_not_permanent() -> None:
    """리포트는 나중에 도착할 수 있다 — 영구 skip으로 굳히지 않는다."""
    assert SkipReason.REPORT_MISSING.permanent is False


def test_title_carries_the_issue_number_and_hypothesis() -> None:
    """PR 목록에서 어느 실험인지 바로 보여야 한다."""
    title = build_pull_request_title(_Experiment())

    assert title.startswith("[AR] ")
    assert "689" in title
    assert "learning_rate" in title


def test_title_stays_within_the_github_limit() -> None:
    """GitHub PR 제목 상한은 256자다. 넘기면 생성이 422로 거부된다."""
    experiment = _Experiment(issue_title="가" * 400)

    title = build_pull_request_title(experiment)

    assert len(title) <= 256


def test_body_embeds_the_agent_report() -> None:
    """본문은 에이전트가 쓴 `report.md` 그대로다 — 요약해서 판단을 흐리지 않는다."""
    experiment = _Experiment(report_markdown="## 결론\n\n개선이 없었다.")

    body = build_pull_request_body(experiment)

    assert "## 결론" in body
    assert "개선이 없었다." in body


def test_body_links_the_experiment_issue_without_closing_it() -> None:
    """이슈를 자동으로 닫지 않는다 — 머지 여부와 실험 종료는 사람이 정한다."""
    body = build_pull_request_body(_Experiment())

    assert "#689" in body
    for keyword in ("Closes #689", "Fixes #689", "Resolves #689"):
        assert keyword not in body


def test_metadata_key_is_stable() -> None:
    """멱등 판정이 이 키에 걸려 있다. 바뀌면 이미 만든 PR을 다시 만든다."""
    assert PULL_REQUEST_METADATA_KEY == "pull_request_number"
    assert len(PULL_REQUEST_METADATA_KEY) <= 64


def test_body_is_truncated_to_the_github_limit() -> None:
    """GitHub PR body 상한은 65,536자인데 `report_markdown`은 262,144자까지 허용된다.

    자르지 않으면 422가 나고, 그 문구는 exists/no_commits 어디에도 안 걸려 일반 실패로
    떨어진다. 그건 기록하지 않는 쪽이라 그 실험이 매 주기 같은 422를 되풀이하며 **끝내
    PR을 얻지 못한다.**
    """
    experiment = _Experiment(report_markdown="가" * 200_000)

    body = build_pull_request_body(experiment)

    assert len(body) <= 65_536


def test_a_truncated_body_says_so_and_points_at_the_original() -> None:
    """잘린 것을 알리지 않으면 리포트가 원래 그렇게 끝난 줄로 읽힌다."""
    experiment = _Experiment(report_markdown="가" * 200_000)

    body = build_pull_request_body(experiment)

    assert "잘렸습니다" in body
    assert "report.md" in body


def test_a_short_body_is_left_alone() -> None:
    experiment = _Experiment(report_markdown="## 결론\n\n짧다.")

    body = build_pull_request_body(experiment)

    assert "잘렸습니다" not in body
