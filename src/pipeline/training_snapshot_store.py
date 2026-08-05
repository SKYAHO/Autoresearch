"""학습 데이터셋 스냅샷의 GCS 게시·조회 스토어(#530).

[파이프라인] 조립이 만든 CSV·sidecar를 content-addressed 불변 객체로 GCS에 올리고,
재사용 학습이 그것을 다시 내려받는 구간을 담당한다.

[기능] ``gs://<root>/by-hash/<dataset_sha256>/`` 레이아웃, ``if_generation_match=0``
기반 write-once 게시(이미 있으면 no-op), ``by-date/dt=<날짜>/<service>.json`` 포인터의
read-modify-write 갱신, 스냅샷 다운로드를 제공한다. 재시도 단위는 **게시 호출 전체**다 —
write-once가 412를 no-op으로 흡수하므로 CSV만 올라간 상태에서 재시도하면 manifest부터
자연스럽게 이어진다.

[비책임] 데이터셋 조립(build_training_dataset), 모델 학습(train), manifest 형식
정의(training_provenance)는 다루지 않는다. 게시 여부 판단(실험 조립인지, 루트가
지정됐는지)도 호출부의 몫이다.
"""

from __future__ import annotations

import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from src.pipeline.training_provenance import (
    MAX_POINTER_HISTORY,
    SnapshotPointerEntry,
    TrainingSnapshotManifest,
    TrainingSnapshotPointer,
    load_training_snapshot_manifest,
    sha256_file,
    snapshot_manifest_path,
)

CSV_OBJECT_NAME = "training_dataset.csv"
MANIFEST_OBJECT_NAME = "snapshot_manifest.json"
_PRECONDITION_FAILED = 412
_NOT_FOUND = 404
_RETRY_BASE_SECONDS = 1.0


class SnapshotStoreError(RuntimeError):
    """스냅샷 게시 또는 조회가 확정적으로 실패했음을 알리는 오류."""


def _parse_root(root: str) -> tuple[str, str]:
    """gs://bucket/prefix를 (bucket, prefix)로 나눈다."""
    parsed = urlparse(root)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise SnapshotStoreError(
            f"snapshot root는 gs://bucket[/prefix] 형식이어야 합니다: {root}"
        )
    return parsed.netloc, parsed.path.strip("/")


def _join(prefix: str, *parts: str) -> str:
    return "/".join([segment for segment in (prefix, *parts) if segment])


def _is_precondition_failure(error: BaseException) -> bool:
    """write-once 전제조건 위반(이미 존재)인지 판정한다.

    google-cloud-storage가 있는 환경에서는 정확한 예외 타입으로 판정한다. 타입만
    보면 client를 주입한 테스트의 가짜 예외를 받지 못하고, code만 보면 우연히
    code=412를 갖는 무관한 예외까지 "이미 게시됨"으로 삼켜 아무것도 쓰이지 않은
    실행을 성공으로 오인한다. 그래서 둘 다 본다 — 실제 GCS 오류는 타입으로 가리고,
    그 밖의 객체만 code로 판정한다.
    """
    try:
        from google.api_core.exceptions import GoogleAPICallError, PreconditionFailed
    except ImportError:
        pass
    else:
        if isinstance(error, GoogleAPICallError):
            return isinstance(error, PreconditionFailed)
    return getattr(error, "code", None) == _PRECONDITION_FAILED


def _is_not_found(error: BaseException) -> bool:
    """조회 대상 object가 아직 없다는 뜻인지 판정한다.

    ``_is_precondition_failure``와 같은 이유로 타입을 먼저 본다. 최초 게시(포인터
    부재)와 권한·네트워크 오류를 구별하지 못하면, 읽기 실패를 "아직 없음"으로 오인해
    IAM 설정 누락 같은 실제 문제가 조용히 묻힌다.
    """
    try:
        from google.api_core.exceptions import GoogleAPICallError, NotFound
    except ImportError:
        pass
    else:
        if isinstance(error, GoogleAPICallError):
            return isinstance(error, NotFound)
    return isinstance(error, FileNotFoundError) or getattr(error, "code", None) == _NOT_FOUND


def _resolve_client(client: object | None) -> object:
    if client is not None:
        return client
    from google.cloud import storage

    return storage.Client()


def _upload_once(bucket: object, name: str, *, path: Path | None, text: str | None) -> None:
    """write-once로 올리되 이미 있으면 no-op으로 흡수한다."""
    blob = bucket.blob(name)
    try:
        if path is not None:
            blob.upload_from_filename(str(path), if_generation_match=0)
        else:
            blob.upload_from_string(
                text, content_type="application/json", if_generation_match=0
            )
    except Exception as error:
        if _is_precondition_failure(error):
            return
        raise


def publish_snapshot(
    *,
    dataset_path: Path,
    snapshot_root: str,
    record_pointer: bool,
    client: object | None = None,
    max_attempts: int = 3,
) -> str:
    """CSV와 sidecar를 content-addressed 주소에 게시하고 by-hash prefix URI를 돌려준다.

    Args:
        record_pointer: by-date 포인터도 갱신할지. 실험 조립은 False로 넘겨야 한다 —
            prod 포인터를 오염시키지 않기 위해서다(#530 §6.3).
        max_attempts: 게시 호출 전체의 재시도 횟수.

    Raises:
        SnapshotStoreError: 재시도를 소진하고도 실패하면. 로컬 CSV는 유효하게 남아
            있으므로 메시지에 그 경로를 싣는다.
    """
    manifest = load_training_snapshot_manifest(dataset_path)
    dataset_sha256 = sha256_file(dataset_path)
    bucket_name, prefix = _parse_root(snapshot_root)
    object_prefix = _join(prefix, "by-hash", dataset_sha256)
    uri = f"gs://{bucket_name}/{object_prefix}/"

    last_error: BaseException | None = None
    for attempt in range(1, max_attempts + 1):
        try:
            resolved = _resolve_client(client)
            bucket = resolved.bucket(bucket_name)
            _upload_once(
                bucket, f"{object_prefix}/{CSV_OBJECT_NAME}", path=dataset_path, text=None
            )
            _upload_once(
                bucket,
                f"{object_prefix}/{MANIFEST_OBJECT_NAME}",
                path=snapshot_manifest_path(dataset_path),
                text=None,
            )
            if record_pointer:
                _update_pointer(
                    bucket,
                    prefix=prefix,
                    manifest=manifest,
                    dataset_sha256=dataset_sha256,
                    uri=uri,
                )
            return uri
        except (SnapshotStoreError, NotImplementedError):
            # 재시도는 일시적 I/O 장애를 위한 것이다. 프로그래밍 오류를 재시도하면
            # 같은 실패를 max_attempts번 반복하며 백오프만 소모하고, 원래 예외가
            # SnapshotStoreError로 덮여 원인이 가려진다.
            raise
        except Exception as error:  # noqa: BLE001 - 재시도 후 확정 실패로 감싼다
            last_error = error
            if attempt < max_attempts:
                time.sleep(_RETRY_BASE_SECONDS * (2 ** (attempt - 1)))

    raise SnapshotStoreError(
        f"스냅샷 게시가 {max_attempts}회 시도 후 실패했습니다: {uri}. "
        f"로컬 CSV는 유효하게 저장돼 있습니다: {dataset_path} "
        "(파드가 살아있는 동안 이 파일을 직접 회수할 수 있습니다)."
    ) from last_error


def _pointer_object_name(prefix: str, manifest: TrainingSnapshotManifest) -> str:
    return _join(
        prefix,
        "by-date",
        f"dt={manifest.events_end_date.isoformat()}",
        f"{manifest.feature_service}.json",
    )


def _update_pointer(
    bucket: object,
    *,
    prefix: str,
    manifest: TrainingSnapshotManifest,
    dataset_sha256: str,
    uri: str,
) -> None:
    """by-date 포인터를 최신 스냅샷으로 갱신한다.

    read-modify-write에 generation 전제조건을 걸어, 같은 좌표에 동시에 쓰는 실행이
    서로의 갱신을 덮어쓰지 않게 한다. 경합하면 호출부의 재시도가 다시 읽고 시도한다.
    """
    name = _pointer_object_name(prefix, manifest)
    blob = bucket.blob(name)

    current: TrainingSnapshotPointer | None = None
    generation: int | None = None
    try:
        blob.reload()
        generation = blob.generation
        current = TrainingSnapshotPointer.model_validate_json(
            blob.download_as_bytes().decode("utf-8")
        )
    except Exception as error:  # noqa: BLE001 - 포인터 부재만 최초 게시로 삼는다
        if not _is_not_found(error):
            raise
        current = None
        generation = None

    if current is not None and current.dataset_sha256 == dataset_sha256:
        return

    history: list[SnapshotPointerEntry] = []
    if current is not None:
        history = [
            SnapshotPointerEntry(
                dataset_sha256=current.dataset_sha256,
                published_at=current.published_at,
            ),
            *current.previous,
        ][:MAX_POINTER_HISTORY]

    pointer = TrainingSnapshotPointer(
        dataset_sha256=dataset_sha256,
        uri=uri,
        events_start_date=manifest.events_start_date,
        events_end_date=manifest.events_end_date,
        feature_service=manifest.feature_service,
        registry_generation=manifest.registry_generation,
        published_at=datetime.now(timezone.utc),
        previous=history,
    )
    blob.upload_from_string(
        pointer.model_dump_json(indent=2),
        content_type="application/json",
        if_generation_match=0 if generation is None else generation,
    )
