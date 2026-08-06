"""실험 candidate 이미지·코드 참조 결정의 계약을 검증한다.

[파이프라인] ②candidate Job을 만들기 직전 — 의존성 diff에 따라 실험 이미지를 굽거나
기존 dev 이미지를 재사용하기로 결정하는 경계를 검증한다.

[기능] 결정 결과 타입의 불변식, 환경 설정 검증, 워크플로우 run·job conclusion 조합에
대한 판정과 dispatch 멱등성을 검증한다.

[비책임] GitHub Actions 러너에서 도는 diff 판단·아카이브 업로드·이미지 빌드는
``tests/test_experiment_build_workflow.py``와 실제 워크플로우 실행의 검증 범위다.
"""

from __future__ import annotations

import pytest

from agent_orchestration.experiment_build.config import (
    ExperimentBuildConfigError,
    ExperimentBuildSettings,
)
from agent_orchestration.experiment_build.contracts import (
    CandidateRuntime,
    ImageBuildState,
)


CANDIDATE_SHA = "c" * 40
BASE_DEV_SHA = "d" * 40
FEAST_IMAGE_URI = "asia-northeast3-docker.pkg.dev/example/ar/autoresearch-feast"
DEV_FEAST_IMAGE = f"{FEAST_IMAGE_URI}@sha256:{'e' * 64}"


def _settings(**overrides: str) -> ExperimentBuildSettings:
    """검증을 통과하는 기본 설정에 일부 필드만 바꿔 만든다."""
    values = {
        "github_repository": "SKYAHO/Autoresearch",
        "feast_image_uri": FEAST_IMAGE_URI,
        "dev_feast_image": DEV_FEAST_IMAGE,
    }
    values.update(overrides)
    return ExperimentBuildSettings(**values)


def test_ready_runtime_requires_both_references() -> None:
    with pytest.raises(ValueError, match="image_ref"):
        CandidateRuntime(state=ImageBuildState.READY, image_ref=None)


def test_ready_runtime_requires_code_archive_sha() -> None:
    with pytest.raises(ValueError, match="code_archive_sha"):
        CandidateRuntime(
            state=ImageBuildState.READY,
            image_ref=DEV_FEAST_IMAGE,
            code_archive_sha=None,
        )


def test_pending_runtime_must_not_carry_references() -> None:
    with pytest.raises(ValueError, match="references"):
        CandidateRuntime(
            state=ImageBuildState.BUILD_PENDING,
            image_ref=DEV_FEAST_IMAGE,
            code_archive_sha=CANDIDATE_SHA,
        )


def test_pending_runtime_without_references_is_valid() -> None:
    runtime = CandidateRuntime(state=ImageBuildState.BUILD_PENDING)

    assert runtime.image_ref is None
    assert runtime.code_archive_sha is None


@pytest.mark.parametrize(
    ("field_name", "invalid_value"),
    [
        ("github_repository", "SKYAHO/Autoresearch/extra"),
        ("feast_image_uri", ""),
        ("dev_feast_image", "   "),
        ("workflow_file", "experiment-image.txt"),
        ("workflow_ref", ""),
    ],
)
def test_settings_reject_invalid_values(field_name: str, invalid_value: str) -> None:
    with pytest.raises(ExperimentBuildConfigError, match=field_name):
        _settings(**{field_name: invalid_value})


def test_settings_read_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch")
    monkeypatch.setenv("ORCH_EXPERIMENT_FEAST_IMAGE_URI", FEAST_IMAGE_URI)
    monkeypatch.setenv("ORCH_DEV_FEAST_IMAGE", DEV_FEAST_IMAGE)

    settings = ExperimentBuildSettings.from_environment()

    assert settings == _settings()
    assert settings.workflow_file == "experiment-image.yml"
    assert settings.workflow_ref == "main"


def test_settings_reject_missing_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("ORCH_EXPERIMENT_FEAST_IMAGE_URI", raising=False)
    monkeypatch.setenv("ORCH_GITHUB_REPOSITORY", "SKYAHO/Autoresearch")
    monkeypatch.setenv("ORCH_DEV_FEAST_IMAGE", DEV_FEAST_IMAGE)

    with pytest.raises(ExperimentBuildConfigError, match="ORCH_EXPERIMENT_FEAST_IMAGE_URI"):
        ExperimentBuildSettings.from_environment()
