"""training_snapshot_store의 GCS 레이아웃·write-once 의미론 단위 테스트(#530).

실제 GCS를 부르지 않는다 — _download_pinned_registry와 같은 client 주입 패턴으로
가짜 client를 넘긴다.
"""

from __future__ import annotations

import json
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

    def upload_from_filename(
        self, filename: str, *, if_generation_match: int | None = None, **_: object
    ) -> None:
        self._write(Path(filename).read_bytes(), if_generation_match)

    def upload_from_string(
        self, data: bytes | str, *, if_generation_match: int | None = None, **_: object
    ) -> None:
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


def _republish_sidecar(csv_path: Path) -> None:
    """CSV를 수정한 뒤 sha가 맞는 sidecar를 다시 쓴다."""
    from src.pipeline.training_provenance import (
        build_snapshot_manifest,
        RegistryProvenance,
        snapshot_manifest_path,
        write_manifest_atomic,
    )

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


@pytest.mark.parametrize(
    ("root", "expected_prefix"),
    [
        ("gs://snapshots", ""),
        ("gs://snapshots/training", "training"),
        ("gs://snapshots/training/", "training"),
    ],
)
def test_publish_normalizes_root_prefix(tmp_path, root, expected_prefix) -> None:
    """루트에 prefix가 없거나 끝에 슬래시가 있어도 object 이름이 어긋나지 않는다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()

    store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root=root,
        record_pointer=False,
        client=client,
    )

    from src.pipeline.training_provenance import sha256_file

    sha = sha256_file(csv_path)
    head = f"{expected_prefix}/" if expected_prefix else ""
    assert f"{head}by-hash/{sha}/training_dataset.csv" in client.buckets["snapshots"].objects


def test_pointer_records_latest_and_keeps_previous(tmp_path) -> None:
    """재조립으로 sha가 바뀌면 포인터가 최신을 가리키고 이전 sha를 previous에 남긴다."""
    client = _FakeClient()
    first = _write_dataset(tmp_path)
    store.publish_snapshot(
        dataset_path=first,
        snapshot_root="gs://snapshots/training",
        record_pointer=True,
        client=client,
    )

    second_dir = tmp_path / "second"
    second_dir.mkdir()
    second = _write_dataset(second_dir)
    second.write_text("clicked\n1\n1\n1\n", encoding="utf-8")
    _republish_sidecar(second)
    store.publish_snapshot(
        dataset_path=second,
        snapshot_root="gs://snapshots/training",
        record_pointer=True,
        client=client,
    )

    from src.pipeline.training_provenance import sha256_file

    payload = json.loads(
        client.buckets["snapshots"]
        .objects["training/by-date/dt=2026-08-01/ctr_training_v1.json"][0]
        .decode("utf-8")
    )
    assert payload["dataset_sha256"] == sha256_file(second)
    assert [entry["dataset_sha256"] for entry in payload["previous"]] == [
        sha256_file(first)
    ]


def test_pointer_history_is_capped(tmp_path) -> None:
    """previous는 MAX_POINTER_HISTORY개를 넘지 않는다."""
    from src.pipeline.training_provenance import MAX_POINTER_HISTORY

    client = _FakeClient()
    for index in range(MAX_POINTER_HISTORY + 3):
        run_dir = tmp_path / f"run{index}"
        run_dir.mkdir()
        csv_path = _write_dataset(run_dir)
        csv_path.write_text("clicked\n" + "1\n" * (index + 1), encoding="utf-8")
        _republish_sidecar(csv_path)
        store.publish_snapshot(
            dataset_path=csv_path,
            snapshot_root="gs://snapshots/training",
            record_pointer=True,
            client=client,
        )

    payload = json.loads(
        client.buckets["snapshots"]
        .objects["training/by-date/dt=2026-08-01/ctr_training_v1.json"][0]
        .decode("utf-8")
    )
    assert len(payload["previous"]) == MAX_POINTER_HISTORY


def test_experiment_assembly_does_not_touch_pointer(tmp_path) -> None:
    """record_pointer=False면 by-date 객체가 아예 생기지 않는다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )
    assert not any(
        name.startswith("training/by-date/")
        for name in client.buckets["snapshots"].objects
    )
