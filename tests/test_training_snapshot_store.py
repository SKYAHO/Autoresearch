"""training_snapshot_store의 GCS 레이아웃·write-once 의미론 단위 테스트(#530).

실제 GCS를 부르지 않는다 — _download_pinned_registry와 같은 client 주입 패턴으로
가짜 client를 넘긴다.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from src.pipeline import training_snapshot_store as store


class _PreconditionFailed(Exception):
    """google.api_core.exceptions.PreconditionFailed와 같은 code 속성을 갖는다."""

    code = 412


class _FakeBlob:
    def __init__(self, bucket: "_FakeBucket", name: str) -> None:
        self._bucket = bucket
        self.name = name
        self.generation: int | None = None

    def upload_from_filename(self, filename, *, if_generation_match=None, **_):
        self._write(Path(filename).read_bytes(), if_generation_match)

    def upload_from_string(self, data, *, if_generation_match=None, **_):
        payload = data.encode("utf-8") if isinstance(data, str) else data
        self._write(payload, if_generation_match)

    def _write(self, payload: bytes, if_generation_match) -> None:
        existing = self._bucket.objects.get(self.name)
        if if_generation_match == 0 and existing is not None:
            raise _PreconditionFailed("object already exists")
        if if_generation_match not in (None, 0):
            if existing is None or existing[1] != if_generation_match:
                raise _PreconditionFailed("generation mismatch")
        self._bucket.generation += 1
        self._bucket.objects[self.name] = (payload, self._bucket.generation)
        self.generation = self._bucket.generation

    def download_as_bytes(self) -> bytes:
        return self._bucket.objects[self.name][0]

    def download_to_filename(self, filename) -> None:
        Path(filename).write_bytes(self.download_as_bytes())

    def reload(self) -> None:
        entry = self._bucket.objects.get(self.name)
        if entry is None:
            raise FileNotFoundError(self.name)
        self.generation = entry[1]

    def exists(self) -> bool:
        return self.name in self._bucket.objects


class _FakeBucket:
    def __init__(self) -> None:
        self.objects: dict[str, tuple[bytes, int]] = {}
        self.generation = 0

    def blob(self, name: str, **_) -> _FakeBlob:
        return _FakeBlob(self, name)


class _FakeClient:
    def __init__(self) -> None:
        self.buckets: dict[str, _FakeBucket] = {}

    def bucket(self, name: str) -> _FakeBucket:
        return self.buckets.setdefault(name, _FakeBucket())


def _write_dataset(tmp_path: Path) -> Path:
    """CSV와 대응 sidecar를 만든 뒤 CSV 경로를 돌려준다."""
    from src.pipeline.training_provenance import (
        build_snapshot_manifest,
        RegistryProvenance,
        snapshot_manifest_path,
        write_manifest_atomic,
    )

    csv_path = tmp_path / "training_dataset.csv"
    csv_path.write_text("clicked\n1\n0\n", encoding="utf-8")
    manifest = build_snapshot_manifest(
        dataset_path=csv_path,
        events_start_date="2026-07-26",
        events_end_date="2026-08-01",
        feature_service="ctr_training_v1",
        registry=RegistryProvenance(
            uri="gs://bucket/registry.db", generation="17", sha256="c" * 64
        ),
        code_archive_sha=None,
        spine_usable_days=7,
    )
    write_manifest_atomic(manifest, snapshot_manifest_path(csv_path))
    return csv_path


def test_publish_writes_csv_and_manifest_under_content_address(tmp_path) -> None:
    """by-hash/<sha>/ 밑에 CSV와 manifest가 올라가야 한다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()

    uri = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )

    from src.pipeline.training_provenance import sha256_file

    sha = sha256_file(csv_path)
    assert uri == f"gs://snapshots/training/by-hash/{sha}/"
    bucket = client.buckets["snapshots"]
    assert f"training/by-hash/{sha}/training_dataset.csv" in bucket.objects
    assert f"training/by-hash/{sha}/snapshot_manifest.json" in bucket.objects


def test_publish_is_idempotent_for_identical_input(tmp_path) -> None:
    """같은 내용을 다시 게시하면 412를 no-op으로 흡수해야 한다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()

    first = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )
    second = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )

    assert first == second
    assert client.buckets["snapshots"].generation == 2  # 두 번째는 아무것도 안 씀


def test_publish_raises_after_exhausting_retries(tmp_path, monkeypatch) -> None:
    """일시 장애가 계속되면 재시도를 소진하고 실패해야 한다."""
    csv_path = _write_dataset(tmp_path)

    class _AlwaysFailing(_FakeClient):
        def bucket(self, name: str):
            raise RuntimeError("transient GCS failure")

    monkeypatch.setattr(store.time, "sleep", lambda _seconds: None)
    with pytest.raises(store.SnapshotStoreError) as error:
        store.publish_snapshot(
            dataset_path=csv_path,
            snapshot_root="gs://snapshots/training",
            record_pointer=False,
            client=_AlwaysFailing(),
            max_attempts=3,
        )
    assert str(csv_path) in str(error.value)
