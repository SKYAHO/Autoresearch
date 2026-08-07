"""Streamlit 사전등록 제출 폼이 서버 계약과 어긋나지 않는지 고정한다.

전체 파이프라인 중 사용자가 채운 값이 Experiment API 요청으로 변환되는 구간을 검증한다.
Streamlit 위젯 렌더링은 담당하지 않는다 — `agent_orchestration.ui.views`는 streamlit
의존성이 필요해 여기서 import하지 않는다.

UI 이미지는 `issue_authoring.py`를 포함하지 않아 서버 모델을 import하지 못하고 보낼
값을 직접 조립한다. 그 조립 결과가 서버와 어긋나면 사용자가 채운 값이 422로 돌아오므로,
`to_fields()`가 서버 검증을 통과한다는 것을 여기서 고정한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_orchestration.app.experiments.issue_authoring import IssueSubmission
from agent_orchestration.ui.client import ExperimentClient
from agent_orchestration.ui.models import IssuePublication, Submission


def _submission(**overrides: Any) -> Submission:
    payload: dict[str, Any] = {
        "title": "views per day ratio feature",
        "hypothesis": "# 주제\n\n비율 피처가 ROC-AUC를 높인다.",
        "allowed_scope": (),
    }
    payload.update(overrides)
    return Submission(**payload)


def test_form_values_are_accepted_by_the_server_contract() -> None:
    """폼이 만든 `fields`가 서버의 `IssueSubmission` 검증을 통과해야 한다."""
    IssueSubmission.model_validate(_submission().to_fields())


def test_submission_without_metrics_is_accepted(
) -> None:
    """지표를 하나도 보내지 않아도 발행 요청이 만들어져야 한다(#570).

    이 성질이 깨지면 마크다운 자유 서술만 채운 사용자가 발행 자체를 못 한다.
    """
    fields = _submission().to_fields()

    assert "primary_metric_name" not in fields
    submission = IssueSubmission.model_validate(fields)
    assert submission.primary_metric_name == ""
    assert submission.change == ""


def test_to_fields_only_sends_keys_the_server_knows() -> None:
    """서버가 모르는 키를 보내면 `extra="forbid"`가 422로 거부한다."""
    assert set(_submission().to_fields()) <= set(IssueSubmission.model_fields)


def test_markdown_hypothesis_round_trips_unchanged() -> None:
    """마크다운 본문이 전송 과정에서 손상되면 이슈에 다른 글이 실린다."""
    body = "# 주제\n\n- `7d_click` 추가\n\n## 검증\n\n1. 재학습\n2. 비교"

    submission = IssueSubmission.model_validate(_submission(hypothesis=body).to_fields())

    assert submission.hypothesis == body


def test_h3_heading_in_hypothesis_is_rejected_before_publication() -> None:
    """`### `는 이슈 본문의 필드 구분자라 값 안에 들어가면 본문 구조가 깨진다."""
    with pytest.raises(ValueError):
        IssueSubmission.model_validate(
            _submission(hypothesis="### 배경\n\n내용").to_fields()
        )


@pytest.mark.parametrize("level", ["#", "##", "####", "#####"])
def test_other_heading_levels_are_allowed(level: str) -> None:
    """구분자와 충돌하지 않는 heading까지 막으면 마크다운을 쓸 수 없다."""
    IssueSubmission.model_validate(
        _submission(hypothesis=f"{level} 배경\n\n내용").to_fields()
    )


@pytest.mark.parametrize(
    "blank,expected",
    [
        ({"title": ""}, "실험 제목"),
        ({"hypothesis": "  "}, "가설"),
    ],
)
def test_missing_required_reports_blank_fields(
    blank: dict[str, Any], expected: str
) -> None:
    """빈 칸은 서버 왕복 없이 화면에서 알려준다.

    값을 `strip()`하지 않고 그대로 넣는다 — 공백만 입력한 경우도 미입력으로 잡혀야
    한다는 것이 여기서 고정하려는 성질이다.
    """
    submission = _submission(**blank)

    assert expected in submission.missing_required()


def test_filled_submission_reports_nothing_missing() -> None:
    """제목과 가설만 채우면 더 요구하지 않는다."""
    assert _submission().missing_required() == []


def test_title_without_ascii_is_rejected_by_the_server_contract() -> None:
    """ASCII가 없는 제목은 브랜치 이름이 해시로 굳어 되돌릴 수 없다."""
    with pytest.raises(ValueError):
        IssueSubmission.model_validate(_submission(title="비율 피처 실험").to_fields())


def test_publish_issue_sends_fields_and_scope_separately(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """서버는 `fields`와 `allowed_scope`를 다른 층에서 받는다."""
    client = ExperimentClient("http://127.0.0.1:8000", "t" * 32)
    seen: dict[str, Any] = {}

    def fake_request(method: str, path: str, payload: dict[str, object] | None = None) -> Any:
        seen["method"] = method
        seen["path"] = path
        seen["payload"] = payload
        return {
            "issue_number": 533,
            "issue_url": "https://github.com/SKYAHO/Autoresearch/issues/533",
            "issue_branch": "exp/533-views-per-day-ratio-feature",
        }

    monkeypatch.setattr(client, "_request_json", fake_request)
    submission = _submission(allowed_scope=("promotion",))

    result = client.publish_issue(
        "3f2a1c9d-8b7e-4a1f-9c2d-5e6f7a8b9c0d",
        submission.to_fields(),
        submission.allowed_scope,
    )

    assert seen["method"] == "POST"
    assert seen["path"].endswith("/issue")
    assert seen["payload"]["allowed_scope"] == ["promotion"]
    assert seen["payload"]["fields"]["title"] == "views per day ratio feature"
    assert result.issue_number == 533


def test_issue_publication_rejects_a_malformed_response() -> None:
    """응답 형태가 깨지면 화면에 잘못된 링크를 그리지 않고 실패한다."""
    with pytest.raises(ValueError):
        IssuePublication.from_json({"issue_number": "533"})


def test_h3_hypothesis_is_blocked_before_the_first_request() -> None:
    """`### `는 발행 단계에서 422가 되고, 그때는 이미 Experiment가 만들어져 있다.

    UI에는 그 실험을 재발행하는 경로가 없으므로 고아 레코드가 남는다. 마크다운을
    자유롭게 쓰게 한 이상 흔한 입력이라 첫 요청 전에 끊어야 한다.
    """
    problems = _submission(hypothesis="# 주제\n\n### 배경\n\n내용").blocking_problems()

    assert any("###" in problem for problem in problems)


@pytest.mark.parametrize("level", ["#", "##", "####"])
def test_other_heading_levels_are_not_blocked(level: str) -> None:
    """구분자와 겹치지 않는 heading까지 막으면 마크다운을 쓸 수 없다."""
    assert _submission(hypothesis=f"{level} 배경\n\n내용").blocking_problems() == []


def test_blank_fields_are_reported_as_sentences() -> None:
    """화면에 그대로 나가는 문장이므로 항목 이름만 나열하지 않는다."""
    problems = _submission(title="").blocking_problems()

    assert problems == ["실험 제목을(를) 채워 주세요."]
