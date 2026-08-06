"""실험 candidate 런타임 결정 결과의 값 타입 경계.

[파이프라인] ②candidate Job manifest를 조립하기 직전 — 어떤 이미지와 어떤 코드
아카이브로 실행할지가 확정됐는지, 아직 이미지 빌드를 기다려야 하는지를 나타내는
구간을 담당한다.

[기능] 준비 상태 열거값과, 상태별로 참조 필드가 있어야/없어야 하는 불변식을 강제하는
불변 결과 타입을 제공한다.

[비책임] 상태를 판정하는 규칙(`service`), GitHub API 호출(`workflows`), Experiment 상태
머신으로의 매핑(호출자)은 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class ExperimentBuildError(RuntimeError):
    """실험 이미지 빌드 상태를 신뢰할 수 없다."""


class ImageBuildState(StrEnum):
    """②candidate 파드 실행에 필요한 산출물의 준비 상태."""

    READY = "ready"
    BUILD_PENDING = "build_pending"
    BUILD_FAILED = "build_failed"


@dataclass(frozen=True)
class CandidateRuntime:
    """②candidate Job이 사용할 이미지 참조와 코드 아카이브 SHA."""

    state: ImageBuildState
    image_ref: str | None = None
    code_archive_sha: str | None = None

    def __post_init__(self) -> None:
        """READY에만 참조가 있고 그 외에는 없다는 계약을 fail-closed로 강제한다."""
        if self.state is ImageBuildState.READY:
            if not self.image_ref:
                raise ValueError("READY requires image_ref")
            if not self.code_archive_sha:
                raise ValueError("READY requires code_archive_sha")
            return
        if self.image_ref is not None or self.code_archive_sha is not None:
            raise ValueError(f"{self.state} must not carry references")
