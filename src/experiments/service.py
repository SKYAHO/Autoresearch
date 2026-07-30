"""실험 상태 파생과 수명 규칙.

[파이프라인] 에이전트 실험 축의 도메인 계층. 제출·진행 보고·최종 리포트를
받아 이벤트로 저장하고, 저장된 이벤트로부터 현재 상태(state·stage·progress)를
파생한다.

[제공 기능] 가설 제출(id 발급), 진행 보고 append, 최종 리포트 확정, 상세·목록
조회. 상태는 저장하지 않고 **매번 이벤트에서 계산**한다 — 이벤트가 사실이고
상태는 뷰라는 원칙이다.

[비책임] 파일 입출력(`src/experiments/store.py`), HTTP 계약·상태 코드
(`src/experiments/api.py`), 실험 실행 자체(에이전트 루프 소유).
"""

from __future__ import annotations

import uuid
from collections.abc import Callable
from datetime import UTC, datetime

from src.experiments.schemas import (
    EventKind,
    ExperimentDetail,
    ExperimentEvent,
    ExperimentState,
    ExperimentSummary,
    ExperimentVerdict,
    FinalReport,
    HypothesisSubmission,
    StatusUpdate,
    SubmissionAccepted,
)
from src.experiments.store import ExperimentNotFoundError, JsonlExperimentStore

Clock = Callable[[], datetime]
IdSuffixFactory = Callable[[], str]

_TERMINAL_STATES = frozenset({ExperimentState.SUCCEEDED, ExperimentState.FAILED})


class ExperimentAlreadyFinalizedError(RuntimeError):
    """이미 최종 리포트가 기록된 실험에 다시 보고·리포트를 시도했을 때."""


def _default_clock() -> datetime:
    return datetime.now(UTC)


def _default_id_suffix() -> str:
    return uuid.uuid4().hex[:8]


class ExperimentService:
    """실험 수명 하나를 다루는 서비스. 저장소·시계·id 생성기를 주입받는다."""

    def __init__(
        self,
        store: JsonlExperimentStore,
        *,
        clock: Clock = _default_clock,
        id_suffix_factory: IdSuffixFactory = _default_id_suffix,
    ) -> None:
        self._store = store
        self._clock = clock
        self._id_suffix = id_suffix_factory

    # --- 기본 기능 ① 가설 제출 ---------------------------------------------

    def submit(self, submission: HypothesisSubmission) -> SubmissionAccepted:
        """가설을 접수하고 실험 id를 발급한다."""
        now = self._clock()
        experiment_id = self._new_id(now)
        event = self._store.append(
            experiment_id,
            EventKind.SUBMITTED,
            submission.model_dump(mode="json"),
            at=now,
            create=True,
        )
        return SubmissionAccepted(
            experiment_id=experiment_id,
            state=ExperimentState.SUBMITTED,
            submitted_at=event.at,
        )

    # --- 기본 기능 ② 진행 상태 보고 ----------------------------------------

    def report_status(self, experiment_id: str, update: StatusUpdate) -> ExperimentEvent:
        """에이전트의 진행 보고를 append한다. 종료된 실험이면 거부한다."""
        events = self._store.read_events(experiment_id)
        if self._derive_state(events) in _TERMINAL_STATES:
            raise ExperimentAlreadyFinalizedError(experiment_id)
        return self._store.append(
            experiment_id,
            EventKind.STATUS,
            update.model_dump(mode="json"),
            at=self._clock(),
        )

    # --- 기본 기능 ③ 최종 리포트 -------------------------------------------

    def finalize(self, experiment_id: str, report: FinalReport) -> ExperimentEvent:
        """최종 리포트를 기록한다. 한 실험에 한 번만 허용한다."""
        events = self._store.read_events(experiment_id)
        if self._derive_state(events) in _TERMINAL_STATES:
            raise ExperimentAlreadyFinalizedError(experiment_id)
        return self._store.append(
            experiment_id,
            EventKind.REPORT,
            report.model_dump(mode="json"),
            at=self._clock(),
        )

    # --- 조회 ---------------------------------------------------------------

    def get(self, experiment_id: str) -> ExperimentDetail:
        events = self._store.read_events(experiment_id)
        submitted = self._first(events, EventKind.SUBMITTED)
        if submitted is None:
            # 제출 이벤트 없는 파일은 계약 위반이므로 없는 실험으로 취급한다.
            raise ExperimentNotFoundError(experiment_id)
        submission = HypothesisSubmission.model_validate(submitted.payload)
        last_status = self._last(events, EventKind.STATUS)
        report_event = self._last(events, EventKind.REPORT)
        status = StatusUpdate.model_validate(last_status.payload) if last_status else None
        return ExperimentDetail(
            experiment_id=experiment_id,
            title=submission.title,
            hypothesis=submission.hypothesis,
            submitted_by=submission.submitted_by,
            labels=submission.labels,
            state=self._derive_state(events),
            stage=status.stage if status else None,
            progress=status.progress if status else None,
            submitted_at=submitted.at,
            updated_at=events[-1].at,
            report=FinalReport.model_validate(report_event.payload) if report_event else None,
            events=events,
        )

    def list(self) -> list[ExperimentSummary]:
        """모든 실험 요약을 최신 제출이 앞에 오도록 돌려준다."""
        summaries: list[ExperimentSummary] = []
        for experiment_id in self._store.list_experiment_ids():
            try:
                detail = self.get(experiment_id)
            except ExperimentNotFoundError:
                continue
            summaries.append(
                ExperimentSummary(
                    experiment_id=detail.experiment_id,
                    title=detail.title,
                    state=detail.state,
                    stage=detail.stage,
                    progress=detail.progress,
                    verdict=detail.report.verdict if detail.report else None,
                    submitted_at=detail.submitted_at,
                    updated_at=detail.updated_at,
                    event_count=len(detail.events),
                )
            )
        summaries.sort(key=lambda item: item.submitted_at, reverse=True)
        return summaries

    # --- 내부 ---------------------------------------------------------------

    def _new_id(self, now: datetime) -> str:
        return f"exp_{now.strftime('%Y%m%d')}_{self._id_suffix()}"

    @staticmethod
    def _first(events: list[ExperimentEvent], kind: EventKind) -> ExperimentEvent | None:
        return next((event for event in events if event.kind == kind), None)

    @staticmethod
    def _last(events: list[ExperimentEvent], kind: EventKind) -> ExperimentEvent | None:
        return next((event for event in reversed(events) if event.kind == kind), None)

    @staticmethod
    def _derive_state(events: list[ExperimentEvent]) -> ExperimentState:
        """이벤트에서 상태를 계산한다.

        리포트가 있으면 verdict으로 성공/실패를 가른다 — `error`만 실패이고
        `rejected`는 "가설이 기각됐다"는 정상 종료다(실험은 성공했다).
        """
        report = next((event for event in reversed(events) if event.kind == EventKind.REPORT), None)
        if report is not None:
            verdict = FinalReport.model_validate(report.payload).verdict
            return (
                ExperimentState.FAILED
                if verdict is ExperimentVerdict.ERROR
                else ExperimentState.SUCCEEDED
            )
        if any(event.kind == EventKind.STATUS for event in events):
            return ExperimentState.RUNNING
        return ExperimentState.SUBMITTED
