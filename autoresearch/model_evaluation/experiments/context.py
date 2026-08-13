"""오프라인 실험의 식별자·Registry·artifact 경로를 결정하는 context 계층.

[파이프라인] 가설 이슈와 baseline/candidate 코드가 고정된 뒤, Feast offline
retrieval·학습 Job이 실행되기 직전의 실행 context 구간을 담당한다. 실험 ID와 조건
(baseline|candidate), 커밋 SHA를 검증하고 조건별 Registry URI와 결과 prefix를
결정론적으로 생성해 Airflow 실행 계약에 제공한다.

[기능] ``build_experiment_context``는 조건별 Registry key/URI와 실험 결과 URI를
계산하고, ``ExperimentContext.artifact_uri``는 run별 결과 경로를 만든다.
``parse_registry_key``와 ``registry_uri_matches``는 실행 결과가 선언한 좌표에서
나왔는지 검증한다(#454). 조건 구간이 없는 legacy 경로는 candidate 좌표로만
인정한다(#450/#461이 이미 소비 중인 형식).

[비책임] 실제 GCS object 생성·권한·namespace/Job 오케스트레이션은
``Autoresearch-infra``와 ``Autoresearch-airflow``가 담당한다. Feast 정의와 offline
store 조회 구현은 ``feature_repo`` 및 ``autoresearch.jobs``가 소유한다. 비교 판정과
결과 payload는 ``autoresearch.model_evaluation.paired_experiment``가 소유한다.
"""

from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Final
from urllib.parse import urlparse


BASELINE: Final[str] = "baseline"
CANDIDATE: Final[str] = "candidate"
CONDITIONS: Final[tuple[str, str]] = (BASELINE, CANDIDATE)

_EXPERIMENT_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,31}$")
_SHA = re.compile(r"^[0-9a-f]{40}$")
_RUN_ID = re.compile(r"^[a-z0-9][a-z0-9-]{0,63}$")
_ISSUE_NUMBER = re.compile(r"^[1-9][0-9]*$")
_REGISTRY_FILENAME: Final[str] = "registry.db"


def _normalise_root(value: str, *, field: str) -> str:
    root = value.strip().rstrip("/")
    if not root:
        raise ValueError(f"{field} must not be empty")
    if not root.startswith("gs://"):
        raise ValueError(f"{field} must be a gs:// URI")
    return root


def _require_condition(condition: str) -> str:
    if condition not in CONDITIONS:
        raise ValueError(f"condition must be one of {CONDITIONS}")
    return condition


def build_registry_key(
    *,
    issue_number: int,
    experiment_id: str,
    condition: str,
    source_sha: str,
) -> str:
    """조건별 Registry object key를 만든다.

    baseline과 candidate는 source SHA가 같더라도 서로 다른 key를 갖는다 — 같은
    Registry에 두 조건의 정의를 apply하면 나중 실행이 앞선 정의를 덮어써 "같은
    조건 비교"라는 전제가 조용히 깨진다(#454).
    """
    if issue_number <= 0:
        raise ValueError("issue_number must be positive")
    if not _EXPERIMENT_ID.fullmatch(experiment_id):
        raise ValueError("experiment_id must use lowercase letters, digits, and hyphens")
    _require_condition(condition)
    if not _SHA.fullmatch(source_sha):
        raise ValueError("source_sha must be a 40-character lowercase SHA")
    return (
        f"experiments/{issue_number}/{experiment_id}/{condition}/"
        f"{source_sha}/{_REGISTRY_FILENAME}"
    )


@dataclass(frozen=True, slots=True)
class ExperimentContext:
    """하나의 조건 실행을 고정하는 불변 식별 context."""

    issue_number: int
    experiment_id: str
    condition: str
    source_sha: str
    registry_key: str
    registry_uri: str
    artifact_root: str

    def artifact_uri(self, run_id: str) -> str:
        """실험 결과·로그를 저장할 run 전용 prefix를 반환한다."""
        if not _RUN_ID.fullmatch(run_id):
            raise ValueError("run_id must use lowercase letters, digits, and hyphens")
        prefix = self.registry_key.removesuffix(f"/{_REGISTRY_FILENAME}")
        return f"{self.artifact_root}/{prefix}/{run_id}/"


@dataclass(frozen=True, slots=True)
class RegistryCoordinates:
    """Registry key에서 읽어낸 실험 좌표."""

    issue_number: int
    experiment_id: str
    condition: str
    source_sha: str
    legacy: bool


def build_experiment_context(
    *,
    issue_number: int,
    experiment_id: str,
    condition: str,
    source_sha: str,
    registry_root: str,
    artifact_root: str,
) -> ExperimentContext:
    """조건별 Registry·artifact 경로를 결정론적으로 만든다.

    ``registry_root``와 ``artifact_root``는 bucket URI이고, 함수는 GCS object를
    생성하지 않는다. 실제 생성과 IAM 검증은 실행 Job이 수행한다.
    """
    registry_key = build_registry_key(
        issue_number=issue_number,
        experiment_id=experiment_id,
        condition=condition,
        source_sha=source_sha,
    )
    registry_base = _normalise_root(registry_root, field="registry_root")
    artifact_base = _normalise_root(artifact_root, field="artifact_root")
    return ExperimentContext(
        issue_number=issue_number,
        experiment_id=experiment_id,
        condition=condition,
        source_sha=source_sha,
        registry_key=registry_key,
        registry_uri=f"{registry_base}/{registry_key}",
        artifact_root=artifact_base,
    )


def parse_registry_key(registry_key: str) -> RegistryCoordinates:
    """Registry key에서 실험 좌표를 읽는다.

    조건 구간이 있는 #454 경로와, 조건 구간이 없는 legacy candidate 경로를 모두
    인식한다. legacy 경로는 ``condition="candidate"``, ``legacy=True``로 돌려준다 —
    조건 격리 이전에는 baseline 실행 자체가 없었으므로 baseline이 legacy 형식을
    가질 수는 없다.
    """
    parts = registry_key.strip("/").split("/")
    if len(parts) == 6:
        prefix, issue, experiment_id, condition, source_sha, filename = parts
        legacy = False
    elif len(parts) == 5:
        prefix, issue, experiment_id, source_sha, filename = parts
        condition = CANDIDATE
        legacy = True
    else:
        raise ValueError("registry key must use the experiments/<issue>/... layout")

    if prefix != "experiments" or filename != _REGISTRY_FILENAME:
        raise ValueError("registry key must use the experiments/<issue>/... layout")
    if not _ISSUE_NUMBER.fullmatch(issue):
        raise ValueError("issue_number must be positive")
    if not _EXPERIMENT_ID.fullmatch(experiment_id):
        raise ValueError("experiment_id must use lowercase letters, digits, and hyphens")
    _require_condition(condition)
    if not _SHA.fullmatch(source_sha):
        raise ValueError("source_sha must be a 40-character lowercase SHA")

    return RegistryCoordinates(
        issue_number=int(issue),
        experiment_id=experiment_id,
        condition=condition,
        source_sha=source_sha,
        legacy=legacy,
    )


def registry_uri_matches(
    registry_uri: str,
    *,
    registry_root: str,
    issue_number: int,
    experiment_id: str,
    condition: str,
    source_sha: str,
) -> bool:
    """Registry URI가 선언한 root와 실험 좌표에서 나왔는지 확인한다.

    suffix 비교가 아니라 **정확히 일치**하는 URI를 만들어 대조한다. suffix만 보면
    다른 bucket·다른 스킴·상위 prefix가 붙은 URI가 통과하고, 이중 슬래시나 끝
    슬래시처럼 같은 좌표를 주장하는 다른 object도 걸러지지 않는다.

    검증 실패를 예외가 아니라 ``False``로 돌려준다 — 호출부(paired 비교)는 개별
    사유를 모아 하나의 fail-closed 결과로 만들기 때문이다.
    """
    parsed = urlparse(registry_uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        return False
    # userinfo가 박힌 URI는 그대로 결과 payload와 로그로 나가므로 받지 않는다.
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        return False
    try:
        root = _normalise_root(registry_root, field="registry_root")
        isolated_key = build_registry_key(
            issue_number=issue_number,
            experiment_id=experiment_id,
            condition=condition,
            source_sha=source_sha,
        )
    except ValueError:
        return False

    expected = [f"{root}/{isolated_key}"]
    if condition == CANDIDATE:
        # 조건 격리 이전 형식(#450/#461)도 candidate 좌표로 계속 인정한다.
        expected.append(
            f"{root}/experiments/{issue_number}/{experiment_id}/"
            f"{source_sha}/{_REGISTRY_FILENAME}"
        )
    return registry_uri in expected
