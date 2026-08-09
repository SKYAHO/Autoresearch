"""실험을 수행한 에이전트가 자기 결과를 서술한 `report.md`를 받는 경계.

[파이프라인] `measurement.py`가 `metrics.json`을 낸 뒤부터 `results_store.py`가 산출물을
게시하기 전까지의 구간을 담당한다. 채점 결과와 candidate diff를 clone 밖 결과
디렉터리에 모아 Codex에 넘기고, 돌아온 리포트를 게시 대상으로 확정한다. **이것이 실험의
최종 산출물이다** — `metrics.json`은 숫자일 뿐이고, 가설이 섰는지 무너졌는지는 여기서
서술된다.

[기능] `git diff <base> <candidate>`를 파일로 받아 두고, 가설 원문·채점 결과·diff를
읽으라는 지시로 Codex를 실행한 뒤, 나온 리포트의 존재와 절 구성을 확인한다.

[비책임] 지표의 계산·조립(`evaluate.py`·`measurement.py`), Codex 프로세스 실행과 격리
(`codex_worker.py`), 지시문 문안(`prompt.py`), GCS 게시(`results_store.py`), 실행 결과를
로그로 남기는 일(stage 경계인 `phase2`)은 담당하지 않는다.

[중요] **리포트의 내용이 옳은지 검사하지 않는다.** 숫자와 어긋난 서술은 `metrics.json`·
diff와 대조하는 리뷰가 잡는다(`docs/specs/2026-08-09-agent-authored-experiment-report.md`
결정 2). 여기서 하는 확인은 "무엇이 왔는가"까지이고, 형식이 어긋나도 리포트를 버리지
않는다 — 절 이름이 틀린 리포트가 리포트 없음보다 낫다.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
import subprocess
from typing import Final

from agent_orchestration.executor.codex_worker import (
    CodexExecution,
    CodexRunResult,
    run_codex_execution,
)
from agent_orchestration.executor.prompt import REPORT_SECTIONS, build_report_prompt


REPORT_FILENAME: Final = "report.md"

# Codex에 넘길 candidate diff 파일 이름. `metrics.json`과 같은 디렉터리에 두지만 게시
# 대상은 아니다 — 같은 내용을 candidate commit이 이미 들고 있다.
DIFF_FILENAME: Final = "candidate.diff"

# diff 본문 상한. verifier가 candidate에 허용하는 텍스트 diff 상한과 같은 값이라, 통과한
# candidate라면 잘릴 일이 없다. 그래도 상한을 두는 이유는 여기가 프롬프트 입력이기
# 때문이다 — 상한 없이 넘기면 모델 입력 한도에서 실패하고, 그 실패는 리포트를 통째로
# 잃는다.
_DIFF_MAX_BYTES: Final = 1024 * 1024
_DIFF_TRUNCATION_NOTE: Final = (
    "\n\n[하네스] diff가 상한을 넘어 여기서 잘렸습니다. 아래 내용은 변경의 일부입니다.\n"
)


class ReportError(RuntimeError):
    """리포트 작성 단계 실패 사유다. Git 출력과 자격 증명은 포함하지 않는다."""


@dataclass(frozen=True)
class ReportInput:
    """채점이 끝난 실험 하나로 리포트를 받는 데 필요한 입력이다."""

    repository: Path
    metrics_path: Path
    issue_body: str
    base_dev_sha: str
    candidate_sha: str
    codex_home: Path
    timeout_seconds: int


@dataclass(frozen=True)
class ReportResult:
    """리포트 작성 실행의 결과와 그 산출물 관찰치다.

    `path`가 `None`이면 Codex가 파일을 남기지 않은 것이다. 그것을 예외로 올리지 않는
    이유는 호출부가 **어느 경우에도 지표 게시와 API 보고를 계속해야** 하기 때문이고,
    그러려면 실패 여부와 함께 진단용 출력 tail도 함께 받아야 하기 때문이다.
    """

    path: Path | None
    missing_sections: tuple[str, ...]
    codex: CodexRunResult


def _run_git(repository: Path, *arguments: str) -> bytes:
    """hooks·credential helper를 끊은 argv Git 명령의 stdout만 bytes로 반환한다."""
    try:
        completed = subprocess.run(  # noqa: S603 - argv는 이 모듈이 조립한 고정 목록이다
            (
                "git",
                "-c",
                "core.hooksPath=/dev/null",
                "-c",
                "credential.helper=",
                "-C",
                str(repository),
                *arguments,
            ),
            check=False,
            capture_output=True,
        )
    except OSError as error:
        raise ReportError("git_unavailable") from error
    if completed.returncode != 0:
        # 사유는 접미사 없는 고정 코드로 둔다. `phase2._safe_failure_reason`이
        # `^[a-z][a-z0-9_]*$`에 맞는 값만 남기므로 인자를 붙이면 사유가 통째로 사라진다.
        raise ReportError("git_failed")
    return completed.stdout


def capture_candidate_diff(
    repository: Path, *, base_dev_sha: str, candidate_sha: str
) -> str:
    """두 조건을 가른 코드 변경을 리포트 입력용 텍스트로 만든다.

    diff를 주는 이유는 리포트가 "무엇을 어떻게 바꿨는지"를 서술해야 하고, 그 사실의
    출처가 에이전트의 기억이 아니라 실제 커밋이어야 하기 때문이다.

    Returns:
        `git diff <base> <candidate>` 본문. 상한을 넘으면 앞부분만 남기고 잘린 사실을
        본문에 적는다.
    """
    raw = _run_git(
        repository, "diff", "--no-color", base_dev_sha, candidate_sha, "--"
    )
    if len(raw) > _DIFF_MAX_BYTES:
        return _DIFF_TRUNCATION_NOTE + raw[:_DIFF_MAX_BYTES].decode(
            "utf-8", errors="replace"
        )
    return raw.decode("utf-8", errors="replace")


def missing_report_sections(text: str) -> tuple[str, ...]:
    """리포트에서 계약이 요구한 절 중 빠진 것을 순서대로 돌려준다."""
    return tuple(section for section in REPORT_SECTIONS if section not in text)


def write_experiment_report(config: ReportInput) -> ReportResult:
    """가설·채점 결과·candidate diff를 Codex에 넘겨 `report.md`를 받는다.

    작업 디렉터리는 `metrics.json`이 있는 clone 밖 결과 디렉터리다. clone 안에서 돌리지
    않는 이유는 두 가지다 — 이 시점의 저장소는 이미 push된 candidate이고, 리포트는 git
    커밋 대상이 아니라 GCS 게시 산출물이다(계약 결정 5).

    Args:
        config: 저장소·채점 결과 경로·가설 원문·좌표·Codex 실행 입력.

    Returns:
        리포트 경로(없으면 `None`)와 빠진 절, Codex 실행 결과.
    """
    if not config.metrics_path.is_file():
        raise ReportError("metrics_missing")
    result_directory = config.metrics_path.parent
    (result_directory / DIFF_FILENAME).write_text(
        capture_candidate_diff(
            config.repository,
            base_dev_sha=config.base_dev_sha,
            candidate_sha=config.candidate_sha,
        ),
        encoding="utf-8",
    )
    report_path = result_directory / REPORT_FILENAME
    # 앞선 실행이 남긴 리포트를 이번 실행의 산출물로 오인하지 않게 한다. Job은
    # `backoffLimit=1`로 재시도될 수 있고 workspace volume은 그대로 남는다.
    report_path.unlink(missing_ok=True)
    codex = run_codex_execution(
        CodexExecution(
            working_directory=result_directory,
            prompt=build_report_prompt(
                issue_body=config.issue_body,
                metrics_filename=config.metrics_path.name,
                diff_filename=DIFF_FILENAME,
                report_filename=REPORT_FILENAME,
            ),
            codex_home=config.codex_home,
            timeout_seconds=config.timeout_seconds,
        )
    )
    try:
        written = report_path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        written = ""
    if not written.strip():
        return ReportResult(path=None, missing_sections=REPORT_SECTIONS, codex=codex)
    return ReportResult(
        path=report_path,
        missing_sections=missing_report_sections(written),
        codex=codex,
    )
