"""실험 이미지 빌드 인터페이스의 환경 설정 검증 경계.

[파이프라인] ②candidate Job을 만드는 호출자가 GitHub 워크플로우를 dispatch하고 결과를
읽기 전에, 저장소 좌표와 이미지 참조를 검증된 불변 값으로 바꾸는 구간을 담당한다.

[기능] 저장소 slug, 실험 이미지 GAR 경로, diff가 없을 때 재사용할 dev 이미지 참조,
dispatch 대상 워크플로우 파일·ref를 환경 변수에서 읽고 형식을 검증한다.

[비책임] GitHub 토큰 발급(`github_app`), 설정의 Kubernetes 주입(Autoresearch-infra),
워크플로우 호출 자체(`workflows`)는 담당하지 않는다.
"""

from __future__ import annotations

from dataclasses import dataclass
import os
import re


# `launcher/config.py`의 `_DIGEST_IMAGE_PATTERN`과 같은 계약이지만, 두 패키지의 설정
# 경계를 서로 묶지 않기 위해 import하지 않고 여기에 따로 둔다.
_DIGEST_IMAGE_PATTERN = re.compile(r"^[^\s@]+@sha256:[0-9a-f]{64}$")
_REPOSITORY_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")
_WORKFLOW_FILE_PATTERN = re.compile(r"^[A-Za-z0-9_.-]+\.ya?ml$")


class ExperimentBuildConfigError(ValueError):
    """실험 이미지 빌드 설정이 누락됐거나 형식 계약에 맞지 않는다."""


def _required_environment(name: str) -> str:
    """비어 있지 않은 환경 변수 값을 반환한다."""
    value = os.environ.get(name)
    if value is None or not value.strip():
        raise ExperimentBuildConfigError(f"missing {name}")
    return value


@dataclass(frozen=True)
class ExperimentBuildSettings:
    """실험 이미지 결정에 필요한 불변 설정."""

    github_repository: str
    feast_image_uri: str
    dev_feast_image: str
    workflow_file: str = "experiment-image.yml"
    workflow_ref: str = "main"

    def __post_init__(self) -> None:
        """빈 값과 형식 위반을 fail-closed로 거부한다."""
        if _REPOSITORY_PATTERN.fullmatch(self.github_repository) is None:
            raise ExperimentBuildConfigError("invalid github_repository")
        if _WORKFLOW_FILE_PATTERN.fullmatch(self.workflow_file) is None:
            raise ExperimentBuildConfigError("invalid workflow_file")
        required_strings = {
            "feast_image_uri": self.feast_image_uri,
            "dev_feast_image": self.dev_feast_image,
            "workflow_ref": self.workflow_ref,
        }
        for name, value in required_strings.items():
            if not value.strip():
                raise ExperimentBuildConfigError(f"invalid {name}")
        # `dev_feast_image`는 이미 발행된 dev 이미지이므로 저장소 관례대로 digest로
        # 고정한다. 태그 예외는 실험 이미지(`exp-<sha>`)에만 적용된다(spec §3.3).
        if _DIGEST_IMAGE_PATTERN.fullmatch(self.dev_feast_image) is None:
            raise ExperimentBuildConfigError("invalid dev_feast_image")

    @classmethod
    def from_environment(cls) -> ExperimentBuildSettings:
        """호출자 환경에서 필수 설정을 읽고 기본 워크플로우 좌표를 적용한다."""
        return cls(
            github_repository=_required_environment("ORCH_GITHUB_REPOSITORY"),
            feast_image_uri=_required_environment("ORCH_EXPERIMENT_FEAST_IMAGE_URI"),
            dev_feast_image=_required_environment("ORCH_DEV_FEAST_IMAGE"),
        )
