"""오프라인 실험의 식별자·Registry·artifact 경로를 결정하는 context 계층.

[파이프라인] 가설 이슈와 candidate 코드가 고정된 뒤, Feast offline retrieval·학습
Job이 실행되기 직전의 실행 context 구간을 담당한다. 실험 ID와 커밋 SHA를 검증하고
실험별 Registry URI와 결과 prefix를 결정론적으로 생성해 Airflow 실행 계약에 제공한다.

[기능] ``build_experiment_context``는 Registry key/URI와 실험 결과 URI를 계산하며,
``ExperimentContext.artifact_uri``는 run별 결과 경로를 만든다.

[비책임] 실제 GCS object 생성·권한·namespace/Job 오케스트레이션은
``Autoresearch-infra``와 ``Autoresearch-airflow``가 담당한다. Feast 정의와 offline
store 조회 구현은 ``feature_repo`` 및 ``autoresearch.jobs``가 소유한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re


_EXPERIMENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")


def _normalise_root(value: str, *, field: str) -> str:
    root = value.strip().rstrip("/")
    if not root:
        raise ValueError(f"{field} must not be empty")
    if not root.startswith("gs://"):
        raise ValueError(f"{field} must be a gs:// URI")
    return root


@dataclass(frozen=True, slots=True)
class ExperimentContext:
    """하나의 candidate 실행을 고정하는 불변 식별 context."""

    issue_number: int
    experiment_id: str
    candidate_sha: str
    registry_key: str
    registry_uri: str
    artifact_root: str

    def artifact_uri(self, run_id: str) -> str:
        """실험 결과·로그를 저장할 run 전용 prefix를 반환한다."""
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must use lowercase letters, digits, and hyphens")
        return f"{self.artifact_root}/{self.registry_key.removesuffix('/registry.db')}/{run_id}/"


def build_experiment_context(
    *,
    issue_number: int,
    experiment_id: str,
    candidate_sha: str,
    registry_root: str,
    artifact_root: str,
) -> ExperimentContext:
    """실험별 Registry·artifact 경로를 결정론적으로 만든다.

    ``registry_root``와 ``artifact_root``는 bucket URI이고, 함수는 GCS object를
    생성하지 않는다. 실제 생성과 IAM 검증은 실행 Job이 수행한다.
    """
    if issue_number <= 0:
        raise ValueError("issue_number must be positive")
    if not _EXPERIMENT_ID.fullmatch(experiment_id):
        raise ValueError("experiment_id must use lowercase letters, digits, and hyphens")
    if not _SHA.fullmatch(candidate_sha):
        raise ValueError("candidate_sha must be a 40-character lowercase SHA")

    registry_base = _normalise_root(registry_root, field="registry_root")
    artifact_base = _normalise_root(artifact_root, field="artifact_root")
    registry_key = (
        f"experiments/{issue_number}/{experiment_id}/{candidate_sha}/registry.db"
    )
    return ExperimentContext(
        issue_number=issue_number,
        experiment_id=experiment_id,
        candidate_sha=candidate_sha,
        registry_key=registry_key,
        registry_uri=f"{registry_base}/{registry_key}",
        artifact_root=artifact_base,
    )
