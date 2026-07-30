"""실험 상태 대시보드의 HTTP 계약과 최소 화면.

[파이프라인] 에이전트 실험 축의 노출 계층. 사용자는 가설을 제출하고 진행·리포트를
조회하며, 에이전트는 진행 보고와 최종 리포트를 이 API로 올린다.

[제공 기능] `/experiments` 라우터(제출·보고·리포트·조회·목록)와 의존성 없는
최소 HTML 대시보드, 그리고 단독 실행용 앱 팩토리를 제공한다.

[비책임] 상태 파생 규칙(`src/experiments/service.py`), 저장
(`src/experiments/store.py`), 인증·외부 노출(`SKYAHO/Autoresearch-infra`의
oauth2-proxy + 내부 ILB 패턴 — 이 앱은 인증을 구현하지 않는다), 리랭킹 서빙
런타임(`src/serving/app.py` — 별 앱으로 분리해 추론 지연 표면을 건드리지 않는다).
"""

from __future__ import annotations

import html
from typing import Annotated

from fastapi import APIRouter, FastAPI, HTTPException, Path, status
from fastapi.responses import HTMLResponse

from src.experiments.schemas import (
    ExperimentDetail,
    ExperimentEvent,
    ExperimentSummary,
    FinalReport,
    HypothesisSubmission,
    StatusUpdate,
    SubmissionAccepted,
)
from src.experiments.service import ExperimentAlreadyFinalizedError, ExperimentService
from src.experiments.store import (
    ExperimentNotFoundError,
    InvalidExperimentIdError,
    JsonlExperimentStore,
)

EXPERIMENT_ID_PARAM = Annotated[str, Path(min_length=12, max_length=32)]


def build_service() -> ExperimentService:
    """환경 변수 기반 기본 서비스. 앱 기동 시 한 번 만든다."""
    return ExperimentService(JsonlExperimentStore())


def create_router(service: ExperimentService) -> APIRouter:
    """주입된 서비스로 라우터를 만든다. 다른 앱에 mount할 수 있다.

    서비스는 클로저로 잡는다 — `Depends`를 쓰면 `from __future__ import
    annotations` 때문에 함수 안에서 만든 `Annotated` 별칭이 FastAPI의 타입 해석
    시점(모듈 namespace)에 보이지 않아 쿼리 파라미터로 잡힌다(실측).
    """
    router = APIRouter(prefix="/experiments", tags=["experiments"])

    def _resolve(call, *args):  # noqa: ANN001, ANN202 - 내부 오류 매핑 헬퍼
        try:
            return call(*args)
        except InvalidExperimentIdError as error:
            raise HTTPException(status.HTTP_400_BAD_REQUEST, str(error)) from error
        except ExperimentNotFoundError as error:
            raise HTTPException(
                status.HTTP_404_NOT_FOUND, f"실험을 찾을 수 없습니다: {error}"
            ) from error
        except ExperimentAlreadyFinalizedError as error:
            raise HTTPException(
                status.HTTP_409_CONFLICT, f"이미 종료된 실험입니다: {error}"
            ) from error

    @router.post("", response_model=SubmissionAccepted, status_code=status.HTTP_201_CREATED)
    def submit(submission: HypothesisSubmission) -> SubmissionAccepted:
        return service.submit(submission)

    @router.post("/{experiment_id}/status", response_model=ExperimentEvent)
    def report_status(experiment_id: EXPERIMENT_ID_PARAM, update: StatusUpdate) -> ExperimentEvent:
        return _resolve(service.report_status, experiment_id, update)

    @router.post("/{experiment_id}/report", response_model=ExperimentEvent)
    def finalize(experiment_id: EXPERIMENT_ID_PARAM, report: FinalReport) -> ExperimentEvent:
        return _resolve(service.finalize, experiment_id, report)

    @router.get("", response_model=list[ExperimentSummary])
    def list_experiments() -> list[ExperimentSummary]:
        return service.list()

    @router.get("/{experiment_id}", response_model=ExperimentDetail)
    def get_experiment(experiment_id: EXPERIMENT_ID_PARAM) -> ExperimentDetail:
        return _resolve(service.get, experiment_id)

    @router.get("/ui/dashboard", response_class=HTMLResponse, include_in_schema=False)
    def dashboard() -> HTMLResponse:
        return HTMLResponse(render_dashboard(service.list()))

    return router


def render_dashboard(summaries: list[ExperimentSummary]) -> str:
    """의존성 없는 최소 대시보드. 템플릿 엔진을 새로 들이지 않는다.

    사용자 입력(title·stage)이 그대로 들어오므로 **모든 삽입 값을 이스케이프**한다.
    """
    if summaries:
        rows = "".join(
            "<tr>"
            f"<td><code>{html.escape(item.experiment_id)}</code></td>"
            f"<td>{html.escape(item.title)}</td>"
            f"<td>{html.escape(item.state.value)}</td>"
            f"<td>{html.escape(item.stage or '-')}</td>"
            f"<td>{'-' if item.progress is None else f'{item.progress * 100:.0f}%'}</td>"
            f"<td>{html.escape(item.verdict.value if item.verdict else '-')}</td>"
            f"<td>{html.escape(item.updated_at.isoformat(timespec='seconds'))}</td>"
            "</tr>"
            for item in summaries
        )
        body = (
            "<table><tr><th>실험 id</th><th>제목</th><th>상태</th><th>단계</th>"
            f"<th>진행</th><th>판정</th><th>갱신</th></tr>{rows}</table>"
        )
    else:
        body = "<p>아직 제출된 실험이 없습니다. <code>POST /experiments</code>로 가설을 제출하십시오.</p>"
    return (
        "<!DOCTYPE html><html lang='ko'><head><meta charset='utf-8'>"
        "<title>AutoResearch 실험 상태</title>"
        "<style>body{font-family:-apple-system,'Apple SD Gothic Neo',sans-serif;"
        "max-width:960px;margin:0 auto;padding:24px;line-height:1.6}"
        "table{border-collapse:collapse;width:100%;font-size:.92em}"
        "th,td{border:1px solid #ddd;padding:6px 10px;text-align:left}"
        "code{font-size:.9em}</style></head><body>"
        f"<h1>AutoResearch 실험 상태</h1>{body}</body></html>"
    )


def create_app(service: ExperimentService | None = None) -> FastAPI:
    """단독 실행용 앱. 리랭킹 서빙과 분리된 별 프로세스를 전제로 한다."""
    app = FastAPI(title="AutoResearch Experiment Status", version="0.1.0")
    app.include_router(create_router(service if service is not None else build_service()))

    @app.get("/healthcheck", include_in_schema=False)
    def healthcheck() -> dict[str, str]:
        return {"status": "ok"}

    return app
