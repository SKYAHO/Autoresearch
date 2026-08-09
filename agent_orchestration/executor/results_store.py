"""실험 산출물을 Pod 밖 GCS 실험별 경로에 남기는 경계.

[파이프라인] `measurement.py`가 지표를 조립하고 `report.py`가 에이전트의 리포트를 받은
뒤부터, 사람과 리뷰 에이전트가 결과를 읽기까지의 구간을 담당한다. Pod의 `/workspace`는
emptyDir이라 TTL 후 통째로 사라지므로, 여기서 내보내지 않으면 **측정한 것도 서술한 것도
아무것도 남지 않는다** — 실험 #619가 완주하고도 `metric_summary=null`이었던 이유다.

[기능] `gs://<root>/experiments/<issue>/<experiment_id>/` 아래로 파일을 write-once
업로드하고 게시된 좌표를 돌려준다.

[비책임] 지표 계산(`src/pipeline/evaluate.py`)·조립(`measurement.py`), Experiment API
보고(`api_client.py`), 버킷 생성과 IAM(`Autoresearch-infra`)은 담당하지 않는다.
학습 스냅샷의 `by-hash` 게시는 `src/pipeline/training_snapshot_store.py`가 소유한다 —
그쪽은 내용 주소이고 이쪽은 실험 주소라 레이아웃이 다르다.

[중요] 대상 버킷의 `exp-job` GSA 권한은 `objectCreator`+`objectViewer`이며
**`objectCreator`는 기존 객체 교체를 허용하지 않는다.** 게시된 결과는 같은 Pod에서
도는 에이전트도 덮어쓸 수 없다. 코드의 `if_generation_match=0`은 그 성질을 명시적으로
만들어, 권한이 넓어지더라도 계약이 코드에 남게 한다.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
import re
from typing import Final
from urllib.parse import urlparse


_ROOT_URI_PATTERN: Final = re.compile(r"^gs://[a-z0-9][a-z0-9._-]{1,221}(/[^\s]*)?$")
_EXPERIMENT_ID_PATTERN: Final = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")


class ResultsStoreError(RuntimeError):
    """산출물 게시 실패 사유다. 서명 URL과 자격 증명은 포함하지 않는다."""


@dataclass(frozen=True)
class PublishedObject:
    """게시한 객체 하나의 좌표와, 이번 실행이 실제로 만들었는지 여부."""

    uri: str
    created: bool


def build_experiment_prefix(*, issue_number: int, experiment_id: str) -> str:
    """실험 하나가 소유하는 object key 접두부를 만든다.

    이슈 번호를 앞에 두는 이유는 사람이 이슈에서 결과로 바로 내려올 수 있어야 하기
    때문이다. `experiment_id`까지 붙이는 이유는 한 이슈가 재실행으로 여러 실험을
    가질 수 있고, 그때 결과가 서로 덮이면 안 되기 때문이다.
    """
    if issue_number <= 0:
        raise ResultsStoreError("invalid_issue_number")
    if _EXPERIMENT_ID_PATTERN.fullmatch(experiment_id) is None:
        raise ResultsStoreError("invalid_experiment_id")
    return f"experiments/{issue_number}/{experiment_id}"


def _split_root(root_uri: str) -> tuple[str, str]:
    """`gs://bucket/prefix`를 bucket과 정규화된 prefix로 나눈다."""
    if _ROOT_URI_PATTERN.fullmatch(root_uri.strip()) is None:
        raise ResultsStoreError("invalid_results_root")
    parsed = urlparse(root_uri.strip())
    return parsed.netloc, parsed.path.strip("/")


def _resolve_client(client: object | None) -> object:
    if client is not None:
        return client
    from google.cloud import storage

    return storage.Client()


def _is_already_exists(error: BaseException) -> bool:
    """`if_generation_match=0` 위반인지 — 즉 같은 이름이 이미 있는지 판별한다."""
    try:
        from google.api_core.exceptions import PreconditionFailed
    except ImportError:
        pass
    else:
        if isinstance(error, PreconditionFailed):
            return True
    # 라이브러리 버전에 따라 412를 다른 예외로 싣기도 한다. 상태 코드로도 확인한다.
    return getattr(error, "code", None) == 412


def publish_results(
    root_uri: str,
    files: Mapping[str, Path],
    *,
    issue_number: int,
    experiment_id: str,
    client: object | None = None,
) -> dict[str, PublishedObject]:
    """실험 산출물을 write-once로 올리고 게시 좌표를 돌려준다.

    이미 같은 이름이 있으면 **실패시키지 않고 `created=False`로 표시한다.** Job
    재시도(`backoffLimit=1`)가 같은 실험을 다시 돌릴 수 있는데, 거기서 게시가
    막히면 두 번째 실행은 결과를 하나도 남기지 못한다. 다만 조용히 넘기지도 않는다 —
    재실행 결과가 첫 실행과 다를 수 있으므로, **무엇이 새로 만들어졌고 무엇이 이미
    있었는지**를 호출부가 로그로 남길 수 있게 구분해서 돌려준다.

    Args:
        root_uri: `gs://bucket[/prefix]` 형식의 게시 루트.
        files: 게시할 이름 → 로컬 경로. 이름은 실험 접두부 아래의 상대 경로다.
        issue_number: 가설 이슈 번호.
        experiment_id: Experiment API의 실험 식별자.
        client: 주입용 GCS client. 생략하면 기본 자격 증명으로 만든다.

    Returns:
        이름 → 게시 좌표 매핑.
    """
    if not files:
        raise ResultsStoreError("no_files_to_publish")
    bucket_name, root_prefix = _split_root(root_uri)
    experiment_prefix = build_experiment_prefix(
        issue_number=issue_number, experiment_id=experiment_id
    )
    for name, path in files.items():
        if not name or name.startswith("/") or ".." in Path(name).parts:
            raise ResultsStoreError("invalid_object_name")
        if not path.is_file():
            raise ResultsStoreError("publish_source_missing")

    bucket = _resolve_client(client).bucket(bucket_name)
    published: dict[str, PublishedObject] = {}
    for name, path in files.items():
        key = "/".join(part for part in (root_prefix, experiment_prefix, name) if part)
        blob = bucket.blob(key)
        created = True
        try:
            blob.upload_from_filename(str(path), if_generation_match=0)
        except Exception as error:  # noqa: BLE001 - 아래에서 종류를 가려 재발생한다
            if not _is_already_exists(error):
                # 원문에는 서명 URL이 섞일 수 있어 사유 코드만 남긴다.
                raise ResultsStoreError("publish_failed") from error
            created = False
        published[name] = PublishedObject(
            uri=f"gs://{bucket_name}/{key}", created=created
        )
    return published


def collect_publishable_files(
    *,
    metrics_path: Path,
    report_path: Path | None = None,
    training_output_root: Path | None = None,
) -> dict[str, Path]:
    """게시할 파일 목록을 이름 규칙과 함께 모은다.

    `metrics.json`은 판정 입력이라 **반드시** 싣는다. `report.md`는 실험의 최종
    산출물이지만 없으면 건너뛴다 — 리포트 작성이 실패했다고 **숫자까지 잃는 것은
    손해**이기 때문이다. 학습 산출물은 재현·재측정을 위해 함께 싣되, 없으면 조용히
    건너뛴다 — 학습을 켜지 않은 배포에서도 지표 게시 경로가 끊기지 않아야 한다.
    """
    if not metrics_path.is_file():
        raise ResultsStoreError("metrics_missing")
    files: dict[str, Path] = {"metrics.json": metrics_path}
    if report_path is not None and report_path.is_file():
        files["report.md"] = report_path
    if training_output_root is None or not training_output_root.is_dir():
        return files
    for path in sorted(training_output_root.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(training_output_root).as_posix()
        files[f"training-output/{relative}"] = path
    return files
