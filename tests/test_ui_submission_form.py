"""Streamlit 사전등록 제출 폼이 서버 계약과 어긋나지 않는지 고정한다.

전체 파이프라인 중 사용자가 채운 값이 Experiment API 요청으로 변환되는 구간을 검증한다.
Streamlit 위젯 렌더링은 담당하지 않는다 — `agent_orchestration.ui.views`는 streamlit
의존성이 필요해 여기서 import하지 않는다.

UI 이미지는 `issue_authoring.py`를 포함하지 않아 옵션 값을 import하지 못하고 복제한다.
그 복제본이 서버와 어긋나면 사용자가 채운 값이 422로 돌아오므로, 동일성을 여기서
고정한다.
"""

from __future__ import annotations

from typing import Any

import pytest

from agent_orchestration.app.experiments.issue_authoring import (
    SCOPE_LABELS,
    IssueSubmission,
)
from agent_orchestration.ui.client import ExperimentClient
from agent_orchestration.ui.models import (
    METRIC_DIRECTIONS,
    NONE_VALUE,
    NOT_APPLICABLE,
    SCOPE_CHOICES,
    IssuePublication,
    Submission,
)


def _submission(**overrides: Any) -> Submission:
    payload: dict[str, Any] = {
        "title": "views per day ratio feature",
        "hypothesis": "비율 피처가 ROC-AUC를 높인다.",
        "related_work": "",
        "change": "- 추가 피처: views_per_day = views / (days + 1)",
        "primary_metric_name": "roc_auc",
        "primary_metric_direction": "higher_is_better",
        "minimum_primary_delta": "0.002",
        "guardrail_metric_name": NONE_VALUE,
        "guardrail_metric_direction": NOT_APPLICABLE,
        "maximum_guardrail_regression": NONE_VALUE,
        "secondary_metrics": "",
        "allowed_scope": (),
    }
    payload.update(overrides)
    return Submission(**payload)


def test_form_values_are_accepted_by_the_server_contract() -> None:
    """폼이 만든 `fields`가 서버의 `IssueSubmission` 검증을 통과해야 한다."""
    IssueSubmission.model_validate(_submission().to_fields())


def test_declared_guardrail_round_trips() -> None:
    """guardrail 세 값을 함께 채운 제출도 서버 검증을 통과한다."""
    fields = _submission(
        guardrail_metric_name="logloss",
        guardrail_metric_direction="lower_is_better",
        maximum_guardrail_regression="0.001",
    ).to_fields()

    IssueSubmission.model_validate(fields)


def test_metric_direction_options_match_the_server() -> None:
    """화면 문구는 UI 소유지만 전송 값은 서버 계약이다."""
    for value in METRIC_DIRECTIONS.values():
        IssueSubmission.model_validate(
            _submission(primary_metric_direction=value).to_fields()
        )


def test_unset_guardrail_sentinels_match_the_server() -> None:
    """미선언 guardrail의 sentinel이 어긋나면 서버가 동반 선언 위반으로 거부한다."""
    IssueSubmission.model_validate(
        _submission(
            guardrail_metric_name=NONE_VALUE,
            guardrail_metric_direction=NOT_APPLICABLE,
            maximum_guardrail_regression=NONE_VALUE,
        ).to_fields()
    )


def test_scope_keys_match_the_server_labels() -> None:
    """허용 범위 키가 어긋나면 체크한 범위가 이슈 본문에 반영되지 않는다."""
    assert set(SCOPE_CHOICES) == set(SCOPE_LABELS)


def test_to_fields_covers_every_server_field() -> None:
    """서버가 요구하는 필드를 폼이 하나라도 빠뜨리면 422다."""
    assert set(_submission().to_fields()) == set(IssueSubmission.model_fields)


@pytest.mark.parametrize(
    "blank,expected",
    [
        ({"title": ""}, "실험 제목"),
        ({"hypothesis": "  "}, "연구 가설"),
        ({"change": ""}, "변경할 피처 · 모델"),
        ({"primary_metric_name": ""}, "주 지표 이름"),
        ({"minimum_primary_delta": ""}, "최소 개선폭"),
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


def test_partially_declared_guardrail_is_caught_before_the_server() -> None:
    """이름만 채우고 악화폭을 비우면 서버가 422로 거부한다 — 왕복 전에 잡는다."""
    submission = _submission(
        guardrail_metric_name="logloss",
        guardrail_metric_direction="lower_is_better",
        maximum_guardrail_regression="",
    )

    assert any("최대 악화폭" in name for name in submission.missing_required())


def test_title_without_ascii_is_rejected_by_the_server_contract() -> None:
    """ASCII가 없는 제목은 브랜치 이름이 해시로 굳어 되돌릴 수 없다."""
    with pytest.raises(ValueError):
        IssueSubmission.model_validate(_submission(title="비율 피처 실험").to_fields())


def test_optional_fields_are_not_required() -> None:
    """선행 연구 참조와 보조 관측 지표는 비워도 제출된다."""
    assert _submission(related_work="", secondary_metrics="").missing_required() == []


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
    assert seen["payload"]["fields"]["primary_metric_name"] == "roc_auc"
    assert result.issue_number == 533


def test_issue_publication_rejects_a_malformed_response() -> None:
    """응답 형태가 깨지면 화면에 잘못된 링크를 그리지 않고 실패한다."""
    with pytest.raises(ValueError):
        IssuePublication.from_json({"issue_number": "533"})
