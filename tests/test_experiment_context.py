"""격리 실험 실행 context의 경로·식별자 계약을 검증한다."""

from __future__ import annotations

import pytest

from autoresearch.experiments.context import (
    build_experiment_context,
    build_registry_key,
    parse_registry_key,
    registry_uri_matches,
)


def _context(**overrides: object):
    values: dict[str, object] = {
        "issue_number": 449,
        "experiment_id": "primary",
        "condition": "candidate",
        "source_sha": "a" * 40,
        "registry_root": "gs://registry-bucket",
        "artifact_root": "gs://artifact-bucket",
    }
    values.update(overrides)
    return build_experiment_context(**values)  # type: ignore[arg-type]


def test_context_uses_issue_experiment_condition_and_sha_for_immutable_paths() -> None:
    context = _context()

    assert context.registry_key == (
        "experiments/449/primary/candidate/" + "a" * 40 + "/registry.db"
    )
    assert context.registry_uri == "gs://registry-bucket/" + context.registry_key
    assert context.artifact_uri("run-001") == (
        "gs://artifact-bucket/experiments/449/primary/candidate/" + "a" * 40 + "/run-001/"
    )


def test_baseline_and_candidate_do_not_share_registry_even_with_same_sha() -> None:
    # 같은 SHA를 두 조건이 쓰더라도 Registry object를 공유하면 한쪽의 feast apply가
    # 다른 쪽 정의를 덮어써 비교가 성립하지 않는다(#454 격리 규칙).
    baseline = _context(condition="baseline")
    candidate = _context(condition="candidate")

    assert baseline.registry_uri != candidate.registry_uri
    assert baseline.artifact_uri("run-001") != candidate.artifact_uri("run-001")


def test_different_experiment_contexts_do_not_share_registry_or_artifact_paths() -> None:
    first = _context(experiment_id="primary")
    second = _context(experiment_id="ablation", source_sha="b" * 40)

    assert first.registry_uri != second.registry_uri
    assert first.artifact_uri("run-001") != second.artifact_uri("run-001")


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"issue_number": 0}, "issue_number"),
        ({"experiment_id": "bad/name"}, "experiment_id"),
        ({"condition": "challenger"}, "condition"),
        ({"source_sha": "not-a-sha"}, "source_sha"),
        ({"registry_root": ""}, "registry_root"),
        ({"artifact_root": "s3://bucket"}, "artifact_root"),
    ],
)
def test_context_rejects_invalid_identifiers(kwargs: dict[str, object], message: str) -> None:
    with pytest.raises(ValueError, match=message):
        _context(**kwargs)


def test_run_id_cannot_escape_experiment_artifact_prefix() -> None:
    context = _context()

    with pytest.raises(ValueError, match="run_id"):
        context.artifact_uri("../other-experiment")


def test_build_registry_key_matches_context_key() -> None:
    key = build_registry_key(
        issue_number=449,
        experiment_id="primary",
        condition="baseline",
        source_sha="b" * 40,
    )

    assert key == "experiments/449/primary/baseline/" + "b" * 40 + "/registry.db"


def test_parse_registry_key_reads_condition_isolated_path() -> None:
    coordinates = parse_registry_key(
        "experiments/449/primary/baseline/" + "b" * 40 + "/registry.db"
    )

    assert coordinates.issue_number == 449
    assert coordinates.experiment_id == "primary"
    assert coordinates.condition == "baseline"
    assert coordinates.source_sha == "b" * 40
    assert coordinates.legacy is False


def test_parse_registry_key_accepts_legacy_candidate_path() -> None:
    # #450/#461이 이미 소비 중인 조건 없는 경로는 candidate 좌표로 계속 인정한다.
    coordinates = parse_registry_key("experiments/449/primary/" + "a" * 40 + "/registry.db")

    assert coordinates.condition == "candidate"
    assert coordinates.legacy is True


@pytest.mark.parametrize(
    "registry_key",
    [
        "experiments/449/primary/candidate/" + "a" * 40 + "/registry.sqlite",
        "experiments/449/primary/candidate/not-a-sha/registry.db",
        "experiments/0/primary/candidate/" + "a" * 40 + "/registry.db",
        "experiments/449/Primary/candidate/" + "a" * 40 + "/registry.db",
        "runs/449/primary/candidate/" + "a" * 40 + "/registry.db",
        "experiments/449/primary/challenger/" + "a" * 40 + "/registry.db",
    ],
)
def test_parse_registry_key_rejects_foreign_paths(registry_key: str) -> None:
    with pytest.raises(ValueError):
        parse_registry_key(registry_key)


def test_registry_uri_matches_expected_condition_coordinates() -> None:
    uri = "gs://registry-bucket/experiments/449/primary/candidate/" + "a" * 40 + "/registry.db"

    assert registry_uri_matches(
        uri,
        issue_number=449,
        experiment_id="primary",
        condition="candidate",
        source_sha="a" * 40,
    )


def test_registry_uri_does_not_match_other_condition() -> None:
    uri = "gs://registry-bucket/experiments/449/primary/candidate/" + "a" * 40 + "/registry.db"

    assert not registry_uri_matches(
        uri,
        issue_number=449,
        experiment_id="primary",
        condition="baseline",
        source_sha="a" * 40,
    )


def test_legacy_uri_matches_candidate_but_never_baseline() -> None:
    uri = "gs://registry-bucket/experiments/449/primary/" + "a" * 40 + "/registry.db"

    assert registry_uri_matches(
        uri,
        issue_number=449,
        experiment_id="primary",
        condition="candidate",
        source_sha="a" * 40,
    )
    assert not registry_uri_matches(
        uri,
        issue_number=449,
        experiment_id="primary",
        condition="baseline",
        source_sha="a" * 40,
    )


def test_registry_uri_match_rejects_malformed_uri() -> None:
    assert not registry_uri_matches(
        "gs://registry-bucket/experiments/449/primary/candidate/registry.db",
        issue_number=449,
        experiment_id="primary",
        condition="candidate",
        source_sha="a" * 40,
    )
