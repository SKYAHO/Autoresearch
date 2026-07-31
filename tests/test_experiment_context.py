"""격리 실험 실행 context의 경로·식별자 계약을 검증한다."""

from __future__ import annotations

import pytest

from autoresearch.experiments.context import build_experiment_context


def test_context_uses_issue_experiment_and_sha_for_immutable_paths() -> None:
    context = build_experiment_context(
        issue_number=449,
        experiment_id="primary",
        candidate_sha="a" * 40,
        registry_root="gs://registry-bucket",
        artifact_root="gs://artifact-bucket",
    )

    assert context.registry_key == "experiments/449/primary/" + "a" * 40 + "/registry.db"
    assert context.registry_uri == "gs://registry-bucket/" + context.registry_key
    assert context.artifact_uri("run-001") == (
        "gs://artifact-bucket/experiments/449/primary/" + "a" * 40 + "/run-001/"
    )


def test_different_experiment_contexts_do_not_share_registry_or_artifact_paths() -> None:
    first = build_experiment_context(
        issue_number=449,
        experiment_id="primary",
        candidate_sha="a" * 40,
        registry_root="gs://registry-bucket",
        artifact_root="gs://artifact-bucket",
    )
    second = build_experiment_context(
        issue_number=449,
        experiment_id="ablation",
        candidate_sha="b" * 40,
        registry_root="gs://registry-bucket",
        artifact_root="gs://artifact-bucket",
    )

    assert first.registry_uri != second.registry_uri
    assert first.artifact_uri("run-001") != second.artifact_uri("run-001")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"issue_number": 0}, "issue_number"),
        ({"experiment_id": "bad/name"}, "experiment_id"),
        ({"candidate_sha": "not-a-sha"}, "candidate_sha"),
        ({"registry_root": ""}, "registry_root"),
    ],
)
def test_context_rejects_invalid_identifiers(kwargs: dict[str, object], message: str) -> None:
    values: dict[str, object] = {
        "issue_number": 449,
        "experiment_id": "primary",
        "candidate_sha": "a" * 40,
        "registry_root": "gs://registry-bucket",
        "artifact_root": "gs://artifact-bucket",
    }
    values.update(kwargs)

    with pytest.raises(ValueError, match=message):
        build_experiment_context(**values)  # type: ignore[arg-type]


def test_run_id_cannot_escape_experiment_artifact_prefix() -> None:
    context = build_experiment_context(
        issue_number=449,
        experiment_id="primary",
        candidate_sha="a" * 40,
        registry_root="gs://registry-bucket",
        artifact_root="gs://artifact-bucket",
    )

    with pytest.raises(ValueError, match="run_id"):
        context.artifact_uri("../other-experiment")
