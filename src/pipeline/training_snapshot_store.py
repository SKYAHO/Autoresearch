"""학습 데이터셋 스냅샷의 GCS 게시·조회 스토어(#530).

[파이프라인] 조립이 만든 CSV·sidecar를 content-addressed 불변 객체로 GCS에 올리고,
재사용 학습이 그것을 다시 내려받는 구간을 담당한다.

[기능] ``gs://<root>/by-hash/<dataset_sha256>/`` 레이아웃, ``if_generation_match=0``
기반 write-once 게시(이미 있으면 no-op), ``by-date/dt=<날짜>/<service>.json`` 포인터의
read-modify-write 갱신, 스냅샷 다운로드를 제공한다. 재시도 단위는 **게시 호출 전체**다 —
write-once가 412를 no-op으로 흡수하므로 CSV만 올라간 상태에서 재시도하면 manifest부터
자연스럽게 이어진다.

주소가 곧 CSV의 내용 해시이므로 412를 no-op으로 흡수해도 **CSV 바이트가 같음은
보장된다.** 그러나 같은 주소에 함께 올라가는 ``snapshot_manifest.json``의
``created_at``·``registry_generation``·``registry_sha256``·``code_archive_sha``는
CSV 바이트가 정하지 않는 값이라 이 보장이 미치지 않는다 — 특정 ``by-hash/<sha>/``의
manifest는 그 주소에 **최초로 게시한 실행의 것**으로 영구히 고정된다("first
publisher wins"). CSV 바이트 자체는 영향받지 않으므로 데이터 무결성 문제가 아니라
provenance 정밀도의 한계이며, 더 깊은 해법(manifest를 자기 자신의 해시로 주소화)은
#537에서 추적한다(#530 spec §3.2 참고).

[비책임] 데이터셋 조립(build_training_dataset), 모델 학습(train), manifest 형식
정의(training_provenance)는 다루지 않는다. 게시 여부 판단(실험 조립인지, 루트가
지정됐는지)도 호출부의 몫이다.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from datetime import datetime, timezone
from pathlib import Path
from typing import TypeVar
from urllib.parse import urlparse

from pydantic import ValidationError

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

_T = TypeVar("_T")


class SnapshotStoreError(RuntimeError):
    """스냅샷 게시 또는 조회가 확정적으로 실패했음을 알리는 오류."""


def _retry_with_backoff(
    attempt: Callable[[], _T],
    *,
    max_attempts: int,
    on_exhausted: Callable[[BaseException | None], SnapshotStoreError],
) -> _T:
    """게시·다운로드 호출 전체에 지수 백오프(1s, 2s, ...) 재시도를 적용한다(#530).

    ``publish_snapshot``·``download_snapshot``이 공유하는 재시도 골격이다.
    ``attempt``가 ``SnapshotStoreError``·``NotImplementedError``를 내면 결정적
    오류(프로그래밍 오류 또는 재시도해도 달라지지 않는 검증 실패)로 보고 즉시
    전파한다 — 재시도는 일시적 I/O 장애만을 위한 것이라는 계약을 지킨다. 그 외
    예외만 재시도하고, 소진되면 ``on_exhausted``가 만든 오류를 원인 예외와 함께
    올린다.
    """
    last_error: BaseException | None = None
    for current_attempt in range(1, max_attempts + 1):
        try:
            return attempt()
        except (SnapshotStoreError, NotImplementedError):
            raise
        except Exception as error:  # noqa: BLE001 - 재시도 후 확정 실패로 감싼다
            last_error = error
            if current_attempt < max_attempts:
                time.sleep(_RETRY_BASE_SECONDS * (2 ** (current_attempt - 1)))
    raise on_exhausted(last_error) from last_error


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

    def _attempt() -> str:
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
                bucket_name=bucket_name,
                prefix=prefix,
                manifest=manifest,
                dataset_sha256=dataset_sha256,
                uri=uri,
            )
        return uri

    def _on_exhausted(_last_error: BaseException | None) -> SnapshotStoreError:
        return SnapshotStoreError(
            f"스냅샷 게시가 {max_attempts}회 시도 후 실패했습니다: {uri}. "
            f"로컬 CSV는 유효하게 저장돼 있습니다: {dataset_path} "
            "(파드가 살아있는 동안 이 파일을 직접 회수할 수 있습니다)."
        )

    return _retry_with_backoff(
        _attempt, max_attempts=max_attempts, on_exhausted=_on_exhausted
    )


def download_snapshot(
    *,
    dataset_uri: str,
    destination_dir: Path,
    client: object | None = None,
    max_attempts: int = 3,
) -> Path:
    """by-hash 스냅샷을 내려받아 로컬 CSV 경로를 돌려준다.

    sidecar는 ``snapshot_manifest_path()``가 기대하는 ``<csv>.snapshot.json`` 이름으로
    복원한다 — 그래야 기존 ``load_training_snapshot_manifest()``가 그대로 재사용되고
    byte/schema/row_count 재검증이 따라온다. 별도의 검증 경로를 새로 만들지 않는다.

    ``publish_snapshot``과 같은 지수 백오프 재시도 정책을 쓴다(#530 PR 리뷰) — 이
    경로의 목적 자체가 재조립을 피하는 것이라, 일시적 GCS 오류로 실패하면 재실행
    비용이 조립보다 결코 싸지 않다. 재시도 중 실패한 시도가 남긴 부분 다운로드
    (CSV만 있고 sidecar가 없는 등)는 다음 시도가 같은 파일을 덮어써 정리한다.

    주의(호출자 책임): 재시도를 모두 소진해 최종 실패하면 ``destination_dir``에
    부분적으로 쓰인 CSV가 남을 수 있다. 이 모듈은 그 정리를 보장하지 않는다 —
    현재 호출부(``train.main``)는 ``TemporaryDirectory``를 쓰므로 실패해도
    남은 파일은 그 디렉터리와 함께 정리된다. 이는 호출자의 책임이지 이 함수의
    보장이 아니다.

    Raises:
        SnapshotStoreError: URI 형식이 틀렸으면 즉시(재시도 없음), 또는 재시도를
            소진하고도 실패하면. 후자에는 주소에 대응하는 객체가 없는 경우와
            URI의 sha와 manifest의 ``dataset_sha256``이 다른 경우(둘 다 결정적이라
            재시도 자체는 하지 않지만 이 예외 타입으로 감싸 호출자가 한 타입만
            보게 한다)가 포함된다. sha 불일치 검사가 없으면 content-addressing은
            강제되지 않는 이름 규칙에 불과해진다.
    """
    parsed = urlparse(dataset_uri)
    if parsed.scheme != "gs" or not parsed.netloc:
        raise SnapshotStoreError(
            f"dataset URI는 gs://bucket/... 형식이어야 합니다: {dataset_uri}"
        )
    object_prefix = parsed.path.strip("/")
    segments = object_prefix.split("/")
    if len(segments) < 2 or segments[-2] != "by-hash":
        raise SnapshotStoreError(
            f"dataset URI는 by-hash/<sha>/ 로 끝나야 합니다: {dataset_uri}"
        )
    expected_sha = segments[-1]

    csv_path = destination_dir / CSV_OBJECT_NAME
    sidecar = snapshot_manifest_path(csv_path)

    def _attempt() -> Path:
        resolved = _resolve_client(client)
        bucket = resolved.bucket(parsed.netloc)
        try:
            bucket.blob(f"{object_prefix}/{CSV_OBJECT_NAME}").download_to_filename(
                str(csv_path)
            )
            bucket.blob(f"{object_prefix}/{MANIFEST_OBJECT_NAME}").download_to_filename(
                str(sidecar)
            )
        except Exception as error:  # noqa: BLE001 - not-found만 골라 안내 메시지로 바꾼다
            if _is_not_found(error):
                raise SnapshotStoreError(
                    f"스냅샷을 찾을 수 없습니다: {dataset_uri}"
                ) from error
            raise

        manifest = load_training_snapshot_manifest(csv_path)
        if manifest.dataset_sha256 != expected_sha:
            raise SnapshotStoreError(
                "스냅샷 주소와 manifest의 dataset_sha256이 다릅니다 — "
                f"주소={expected_sha}, manifest={manifest.dataset_sha256}"
            )
        return csv_path

    def _on_exhausted(_last_error: BaseException | None) -> SnapshotStoreError:
        return SnapshotStoreError(
            f"스냅샷 다운로드가 {max_attempts}회 시도 후 실패했습니다: {dataset_uri}."
        )

    return _retry_with_backoff(
        _attempt, max_attempts=max_attempts, on_exhausted=_on_exhausted
    )


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
    bucket_name: str,
    prefix: str,
    manifest: TrainingSnapshotManifest,
    dataset_sha256: str,
    uri: str,
) -> None:
    """by-date 포인터를 최신 스냅샷으로 갱신한다.

    read-modify-write에 generation 전제조건을 걸어, 같은 좌표에 동시에 쓰는 실행이
    서로의 갱신을 덮어쓰지 않게 한다. 경합하면 호출부의 재시도가 다시 읽고 시도한다.

    저장된 포인터 JSON이 손상됐거나 ``previous``가 ``MAX_POINTER_HISTORY``를
    넘어 ``model_validate_json``이 거부하면 ``ValidationError``가 난다. 이는
    재시도해도 결과가 달라지지 않는 결정적 오류이므로(#530 PR 리뷰) 여기서
    ``SnapshotStoreError``로 감싸 즉시 전파한다 — 감싸지 않으면 호출부의 넓은
    ``except Exception``이 이를 일시적 I/O 장애로 오인해 3회 재시도를 소모한다.
    """
    name = _pointer_object_name(prefix, manifest)
    blob = bucket.blob(name)
    pointer_uri = f"gs://{bucket_name}/{name}"

    current: TrainingSnapshotPointer | None = None
    generation: int | None = None
    try:
        blob.reload()
        generation = blob.generation
        current = TrainingSnapshotPointer.model_validate_json(
            blob.download_as_bytes().decode("utf-8")
        )
    except ValidationError as error:
        raise SnapshotStoreError(
            f"저장된 by-date 포인터를 파싱할 수 없습니다: {pointer_uri}. "
            "포인터 객체가 손상되었거나(예: previous 히스토리가 상한을 넘음) 지원하지 "
            "않는 형식입니다. 이 객체를 직접 확인하거나 삭제한 뒤 다시 게시하십시오."
        ) from error
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
