"""training_snapshot_store의 GCS 레이아웃·write-once 의미론 단위 테스트(#530).

실제 GCS를 부르지 않는다 — _download_pinned_registry와 같은 client 주입 패턴으로
가짜 client를 넘긴다.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from google.api_core.exceptions import Forbidden, GoogleAPICallError, PreconditionFailed

from src.pipeline import training_snapshot_store as store


class _PreconditionFailed(PreconditionFailed):
    """실제 ``PreconditionFailed``를 그대로 상속한다(#530 최종 리뷰 fix 4).

    이전에는 ``code`` 속성만 흉내 낸 bare ``Exception``이라 production의
    ``isinstance(error, GoogleAPICallError)`` 분기를 전혀 태우지 못하고 매번 code
    fallback으로만 판정됐다. 실제 타입을 상속해야 이 픽스처들이 production이 실제로
    타는 typed 분기를 검증한다.
    """


class _Forbidden(Forbidden):
    """실제 ``Forbidden``을 그대로 상속한다 — 위와 같은 이유(#530 최종 리뷰 fix 4)."""


class _AmbiguousPreconditionCode(GoogleAPICallError):
    """``PreconditionFailed``와 같은 ``code=412``지만 실제로는 다른 GoogleAPICallError 타입이다.

    타입 판정 없이 code만 봤다면 이 예외도 "이미 게시됨"으로 삼켜졌을 것이다 —
    두 차례 리뷰가 막으려 한 바로 그 회귀를 재현하는 픽스처다.
    """

    code = 412


class _AmbiguousNotFoundCode(GoogleAPICallError):
    """``NotFound``와 같은 ``code=404``지만 실제로는 다른 GoogleAPICallError 타입이다."""

    code = 404


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
        entry = self._bucket.objects.get(self.name)
        if entry is None:
            # 실제 google-cloud-storage가 없는 object에 FileNotFoundError를
            # 던지는 것과 같은 계약이다 — download_snapshot의 not-found 처리를
            # 재현하려면 KeyError가 아니라 이 예외가 나야 한다.
            raise FileNotFoundError(self.name)
        return entry[0]

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
    """previous는 MAX_POINTER_HISTORY개를 넘지 않고, 최신순으로 잘려야 한다."""
    from src.pipeline.training_provenance import MAX_POINTER_HISTORY, sha256_file

    client = _FakeClient()
    csv_paths: list[Path] = []
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
        csv_paths.append(csv_path)

    payload = json.loads(
        client.buckets["snapshots"]
        .objects["training/by-date/dt=2026-08-01/ctr_training_v1.json"][0]
        .decode("utf-8")
    )
    assert len(payload["previous"]) == MAX_POINTER_HISTORY
    # 마지막으로 밀려난 현재 항목이 index 0이고, 가장 오래된 두 건(run0, run1)은
    # 캡에 밀려 빠져야 한다 — 개수만 보면 순서가 뒤집혀도 통과하므로 값까지 본다.
    superseded = list(reversed(csv_paths[-(MAX_POINTER_HISTORY + 1) : -1]))
    assert [entry["dataset_sha256"] for entry in payload["previous"]] == [
        sha256_file(path) for path in superseded
    ]


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


def test_pointer_read_failure_is_not_treated_as_first_publish(tmp_path) -> None:
    """포인터 조회가 권한 오류 등으로 실패하면 최초 게시로 오인하지 않고 전파해야 한다.

    ``_Forbidden``이 실제 ``Forbidden``을 상속하므로(#530 최종 리뷰 fix 4), 이 테스트는
    ``_is_not_found``의 ``isinstance(error, GoogleAPICallError)`` typed 분기를 태운다 —
    code fallback(``getattr(error, "code", None) == 404``)이 아니라 타입으로 "NotFound가
    아니다"가 판정된다.
    """

    class _ForbiddenBlob(_FakeBlob):
        def reload(self) -> None:
            raise _Forbidden("permission denied")

    class _ForbiddenBucket(_FakeBucket):
        def blob(self, name: str, **_) -> _FakeBlob:
            if name.startswith("training/by-date/"):
                return _ForbiddenBlob(self, name)
            return _FakeBlob(self, name)

    class _ForbiddenClient(_FakeClient):
        def bucket(self, name: str) -> _FakeBucket:
            return self.buckets.setdefault(name, _ForbiddenBucket())

    csv_path = _write_dataset(tmp_path)
    client = _ForbiddenClient()

    with pytest.raises(store.SnapshotStoreError):
        store.publish_snapshot(
            dataset_path=csv_path,
            snapshot_root="gs://snapshots/training",
            record_pointer=True,
            client=client,
            max_attempts=1,
        )

    assert not any(
        name.startswith("training/by-date/")
        for name in client.buckets["snapshots"].objects
    )


def test_download_restores_sidecar_naming(tmp_path) -> None:
    """sidecar를 load_training_snapshot_manifest가 기대하는 이름으로 내려받아야 한다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    uri = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )

    destination = tmp_path / "download"
    destination.mkdir()
    local = store.download_snapshot(
        dataset_uri=uri, destination_dir=destination, client=client
    )

    from src.pipeline.training_provenance import load_training_snapshot_manifest

    assert local.name == "training_dataset.csv"
    assert (destination / "training_dataset.csv.snapshot.json").is_file()
    assert load_training_snapshot_manifest(local).spine_usable_days == 7


def test_download_rejects_sha_mismatch_between_uri_and_manifest(tmp_path) -> None:
    """URI의 sha와 manifest.dataset_sha256이 다르면 거부한다."""
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )
    destination = tmp_path / "download"
    destination.mkdir()

    with pytest.raises(store.SnapshotStoreError):
        store.download_snapshot(
            dataset_uri="gs://snapshots/training/by-hash/" + "f" * 64 + "/",
            destination_dir=destination,
            client=client,
        )


# --- typed 예외 판정 회귀 고정(#530 최종 리뷰 fix 4) ---


def test_precondition_fallback_accepts_duck_typed_code() -> None:
    """GoogleAPICallError가 아닌 순수 duck-type 예외는 code fallback으로 판정돼야 한다.

    google-api-core는 hard dependency지만, client 주입 관행상 완전히 무관한 객체가
    ``code`` 속성만 우연히 들고 오는 경우를 위해 fallback 분기를 남겨 뒀다 — 이 분기가
    죽지 않았는지 직접 확인한다.
    """

    class _DuckTypedPreconditionFailed(Exception):
        code = 412

    assert store._is_precondition_failure(_DuckTypedPreconditionFailed("dup"))


def test_not_found_fallback_accepts_duck_typed_code() -> None:
    """위와 같은 이유로 ``_is_not_found``의 code fallback 분기도 확인한다."""

    class _DuckTypedNotFound(Exception):
        code = 404

    assert store._is_not_found(_DuckTypedNotFound("missing"))


def test_precondition_type_check_rejects_lookalike_code_on_publish(
    tmp_path, monkeypatch
) -> None:
    """code만 412인 무관한 GoogleAPICallError는 write-once no-op으로 삼켜지면 안 된다.

    ``_is_precondition_failure``의 ``isinstance(error, GoogleAPICallError)`` →
    ``isinstance(error, PreconditionFailed)`` typed 분기를 태운다. code만 봤다면 이
    케이스도 "이미 게시됨"으로 흡수돼 ``publish_snapshot``이 조용히 URI를 돌려줬을
    것이다 — 실제로는 재시도를 소진하고 실패해야 한다.
    """
    csv_path = _write_dataset(tmp_path)

    class _RaisingBlob(_FakeBlob):
        def upload_from_filename(
            self, filename: str, *, if_generation_match: int | None = None, **_: object
        ) -> None:
            raise _AmbiguousPreconditionCode("ambiguous 412")

    class _RaisingBucket(_FakeBucket):
        def blob(self, name: str, **_) -> _FakeBlob:
            return _RaisingBlob(self, name)

    class _RaisingClient(_FakeClient):
        def bucket(self, name: str) -> _FakeBucket:
            return self.buckets.setdefault(name, _RaisingBucket())

    monkeypatch.setattr(store.time, "sleep", lambda _seconds: None)
    with pytest.raises(store.SnapshotStoreError) as error:
        store.publish_snapshot(
            dataset_path=csv_path,
            snapshot_root="gs://snapshots/training",
            record_pointer=False,
            client=_RaisingClient(),
            max_attempts=1,
        )
    # 삼켜진 게 아니라 재시도 소진 실패로 전파됐다는 사실을 원인 체인으로도 고정한다.
    assert isinstance(error.value.__cause__, _AmbiguousPreconditionCode)


def test_download_propagates_lookalike_not_found_code(tmp_path, monkeypatch) -> None:
    """code만 404인 무관한 GoogleAPICallError는 "스냅샷 없음"으로 감싸이면 안 된다.

    ``_is_not_found``의 typed 분기를 태운다 — code만 봤다면 이 케이스(실제로는
    권한·네트워크 오류일 수 있는 예외)가 즉시 not-found로 오인돼 재시도 없이
    끝난다. 타입으로 정확히 판정하면 not-found가 **아니므로** publish와 같은
    정책으로 재시도를 전부 소진한 뒤에야 ``SnapshotStoreError``로 감싸 전파돼야
    한다.

    이전 버전은 ``max_attempts=1``에 ``__cause__``만 확인했는데, 그 조합으로는
    "재시도가 실제로 돌았는가"를 전혀 관찰하지 못한다 — ``_is_not_found``를
    ``return True``로 바꾸는 변이(mutation)를 넣어도 여전히 같은
    ``SnapshotStoreError(...) from error``가 나와 테스트가 죽지 않는 구멍이
    있었다(#530 PR 재리뷰). 그래서 여기서는 시도 횟수와 ``sleep`` 호출 횟수를
    직접 세어 "타입 오판정 시 재시도 없이 1회 만에 끝난다"는 변화를 고정한다.
    """
    csv_path = _write_dataset(tmp_path)
    attempts = {"count": 0}

    class _RaisingBlob(_FakeBlob):
        def download_to_filename(self, filename) -> None:
            attempts["count"] += 1
            raise _AmbiguousNotFoundCode("ambiguous 404")

    class _RaisingBucket(_FakeBucket):
        def blob(self, name: str, **_) -> _FakeBlob:
            return _RaisingBlob(self, name)

    class _RaisingClient(_FakeClient):
        def bucket(self, name: str) -> _FakeBucket:
            return self.buckets.setdefault(name, _RaisingBucket())

    client = _RaisingClient()
    uri = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )

    sleep_calls: list[float] = []
    monkeypatch.setattr(store.time, "sleep", lambda seconds: sleep_calls.append(seconds))

    destination = tmp_path / "download"
    destination.mkdir()
    with pytest.raises(store.SnapshotStoreError) as error:
        store.download_snapshot(
            dataset_uri=uri, destination_dir=destination, client=client, max_attempts=3
        )
    assert isinstance(error.value.__cause__, _AmbiguousNotFoundCode)
    # not-found로 오판정되면 첫 시도에서 즉시 끝나 시도 1회·sleep 0회가 된다 —
    # 타입 판정이 살아있어야만 3회 모두 시도되고 그 사이 2번 백오프가 있다.
    assert attempts["count"] == 3
    assert len(sleep_calls) == 2


def test_download_does_not_retry_genuine_not_found(tmp_path, monkeypatch) -> None:
    """진짜 NotFound(대응 객체가 실제로 없음)는 재시도하지 않고 즉시 실패해야 한다.

    404는 결정적이다 — 재시도해도 없는 객체가 갑자기 생기지 않는다. 재시도하면
    호출자가 이미 확정된 실패를 기다리며 백오프만 허비한다. ``time.sleep``이
    호출되면 실패하도록 만들어 재시도가 전혀 없었음을 확인한다.
    """

    def _no_sleep_allowed(_seconds: float) -> None:
        raise AssertionError(
            "진짜 not-found는 재시도하면 안 되므로 sleep이 호출되면 안 된다"
        )

    monkeypatch.setattr(store.time, "sleep", _no_sleep_allowed)

    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )
    destination = tmp_path / "download"
    destination.mkdir()

    with pytest.raises(store.SnapshotStoreError) as error:
        store.download_snapshot(
            dataset_uri="gs://snapshots/training/by-hash/" + "f" * 64 + "/",
            destination_dir=destination,
            client=client,
            max_attempts=3,
        )
    assert "찾을 수 없습니다" in str(error.value)


# --- by-date 포인터 경합(#530 §3.3, 최종 리뷰 fix 5) ---


def test_pointer_contention_retry_reads_and_prepends_competitor(
    tmp_path, monkeypatch
) -> None:
    """경쟁 게시자가 먼저 쓴 포인터를 나중에 완주하는 쪽이 지우지 않고 previous에 보존해야 한다.

    시나리오: A가 포인터를 읽을 때는 아직 아무것도 없었다고 믿지만(stale view), 그 사이
    실제로는 경쟁자 B가 이미 게시를 끝내 놓았다. A의 첫 쓰기 시도는
    ``if_generation_match=0``인데 실제 generation이 이미 바뀌어 있어 충돌(412)한다 —
    ``_update_pointer``가 이 충돌을 흡수하지 않으므로 ``publish_snapshot``의 재시도가
    다시 읽고, 이번엔 B의 실제 상태를 보고 그 항목을 ``previous`` 맨 앞에 얹은 채 쓴다.

    확인할 계약은 "먼저 읽은 쪽이 이긴다"가 아니라 "나중에 완주하는 쪽이 이기되, 상대의
    발행 사실은 previous에 남는다"이다 — 회귀가 이 라운드에서 ``previous``를 빈 채로
    덮어쓰면(경쟁자를 조용히 지우면) 이 테스트가 잡아낸다.
    """
    from src.pipeline.training_provenance import sha256_file

    monkeypatch.setattr(store.time, "sleep", lambda _seconds: None)

    class _RacyPointerBlob(_FakeBlob):
        def reload(self) -> None:
            self._bucket.pointer_reload_calls += 1
            if self._bucket.pointer_reload_calls <= 2:
                # 처음 두 번(경쟁자 B의 최초 read, A의 첫 시도 read)은 "아직 없음"으로
                # 강제한다. B의 read는 실제로도 없으므로 자연스러운 결과와 같지만, A의
                # read는 실제로는 B가 이미 써 둔 뒤라 진짜 상태와 다른 stale view를
                # 재현한다 — 그래야 A의 쓰기가 실제 generation과 충돌한다.
                raise FileNotFoundError(self.name)
            super().reload()

    class _RacyPointerBucket(_FakeBucket):
        def __init__(self) -> None:
            super().__init__()
            self.pointer_reload_calls = 0

        def blob(self, name: str, **_) -> _FakeBlob:
            if name.startswith("training/by-date/"):
                return _RacyPointerBlob(self, name)
            return _FakeBlob(self, name)

    class _RacyPointerClient(_FakeClient):
        def bucket(self, name: str) -> _FakeBucket:
            return self.buckets.setdefault(name, _RacyPointerBucket())

    client = _RacyPointerClient()

    competitor_dir = tmp_path / "competitor"
    competitor_dir.mkdir()
    competitor_csv = _write_dataset(competitor_dir)
    competitor_csv.write_text("clicked\n1\n1\n1\n1\n", encoding="utf-8")
    _republish_sidecar(competitor_csv)
    store.publish_snapshot(
        dataset_path=competitor_csv,
        snapshot_root="gs://snapshots/training",
        record_pointer=True,
        client=client,
    )
    competitor_sha = sha256_file(competitor_csv)

    own_dir = tmp_path / "own"
    own_dir.mkdir()
    own_csv = _write_dataset(own_dir)
    own_csv.write_text("clicked\n0\n0\n1\n", encoding="utf-8")
    _republish_sidecar(own_csv)
    uri = store.publish_snapshot(
        dataset_path=own_csv,
        snapshot_root="gs://snapshots/training",
        record_pointer=True,
        client=client,
        max_attempts=3,
    )
    own_sha = sha256_file(own_csv)

    assert uri == f"gs://snapshots/training/by-hash/{own_sha}/"
    payload = json.loads(
        client.buckets["snapshots"]
        .objects["training/by-date/dt=2026-08-01/ctr_training_v1.json"][0]
        .decode("utf-8")
    )
    # 나중에 완주한 A(own)가 최신을 가리킨다 — "먼저 읽은 쪽이 이긴다"가 아니다.
    assert payload["dataset_sha256"] == own_sha
    # 그러나 경쟁자 B는 조용히 지워지지 않고 previous에 보존된다.
    assert [entry["dataset_sha256"] for entry in payload["previous"]] == [
        competitor_sha
    ]


# --- 포인터 파싱 실패는 재시도 대상이 아니다(#530 PR 리뷰 fix 3) ---


def test_pointer_parse_failure_raises_without_retry(tmp_path, monkeypatch) -> None:
    """저장된 포인터가 파싱 불가면 재시도를 소모하지 않고 즉시 실패해야 한다.

    ``ValidationError``는 결정적 오류라 재시도해도 같은 결과가 나온다 — 감싸지 않으면
    ``publish_snapshot``의 넓은 ``except Exception``이 이를 일시적 I/O 장애로 오인해
    1s+2s 백오프를 허비한다. ``time.sleep``이 호출되면 실패하도록 만들어 재시도가
    전혀 일어나지 않았음을 확인하고, 메시지에 포인터 객체의 전체 gs:// 경로가
    들어있는지도 본다 — by-hash URI(내용은 멀쩡함)가 아니라 실제로 손상된 그 객체를
    가리켜야 한다.
    """

    def _no_sleep_allowed(_seconds: float) -> None:
        raise AssertionError(
            "결정적 파싱 실패는 재시도하면 안 되므로 sleep이 호출되지 않아야 한다"
        )

    monkeypatch.setattr(store.time, "sleep", _no_sleep_allowed)

    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    bucket = client.bucket("snapshots")
    pointer_name = "training/by-date/dt=2026-08-01/ctr_training_v1.json"
    # 스키마가 거부할 손상된 포인터를 직접 심어 둔다 — 필수 필드가 전부 빠진 객체.
    bucket.objects[pointer_name] = (b"{}", 1)

    with pytest.raises(store.SnapshotStoreError) as error:
        store.publish_snapshot(
            dataset_path=csv_path,
            snapshot_root="gs://snapshots/training",
            record_pointer=True,
            client=client,
            max_attempts=3,
        )
    assert f"gs://snapshots/{pointer_name}" in str(error.value)


# --- download_snapshot 재시도(#530 PR 리뷰 fix 4) ---


def test_download_retries_transient_error_then_succeeds(tmp_path, monkeypatch) -> None:
    """일시적 GCS 오류는 재시도 끝에 성공해야 한다 — publish와 같은 정책이다.

    처음 두 시도는 실패하고 세 번째 시도에서 성공한다는 사실 자체가 재시도 루프가
    실제로 도는지 확인한다.
    """
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    uri = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )
    real_bucket = client.buckets["snapshots"]
    call_count = {"csv": 0}

    class _FlakyCsvBlob(_FakeBlob):
        def download_to_filename(self, filename) -> None:
            call_count["csv"] += 1
            if call_count["csv"] <= 2:
                raise RuntimeError("transient GCS failure")
            super().download_to_filename(filename)

    class _FlakyBucket:
        def blob(self, name: str, **_):
            if name.endswith(store.CSV_OBJECT_NAME):
                return _FlakyCsvBlob(real_bucket, name)
            return real_bucket.blob(name)

    class _FlakyClient:
        def bucket(self, name: str):
            return _FlakyBucket()

    monkeypatch.setattr(store.time, "sleep", lambda _seconds: None)
    destination = tmp_path / "download"
    destination.mkdir()
    local = store.download_snapshot(
        dataset_uri=uri,
        destination_dir=destination,
        client=_FlakyClient(),
        max_attempts=3,
    )

    assert local.name == "training_dataset.csv"
    assert call_count["csv"] == 3


def test_download_raises_after_exhausting_retries(tmp_path, monkeypatch) -> None:
    """일시 장애가 계속되면 재시도를 소진하고 하나의 예외 타입(SnapshotStoreError)으로 실패한다.

    호출자가 raw google 예외 대신 이 모듈 하나의 오류 타입만 처리하면 되게 하는 게
    목적이다 — 원래 예외는 원인 체인에 남는다.
    """
    csv_path = _write_dataset(tmp_path)
    client = _FakeClient()
    uri = store.publish_snapshot(
        dataset_path=csv_path,
        snapshot_root="gs://snapshots/training",
        record_pointer=False,
        client=client,
    )

    class _AlwaysFailingClient:
        def bucket(self, name: str):
            raise RuntimeError("transient GCS failure")

    monkeypatch.setattr(store.time, "sleep", lambda _seconds: None)
    destination = tmp_path / "download"
    destination.mkdir()

    with pytest.raises(store.SnapshotStoreError) as error:
        store.download_snapshot(
            dataset_uri=uri,
            destination_dir=destination,
            client=_AlwaysFailingClient(),
            max_attempts=3,
        )
    assert uri in str(error.value)
    assert isinstance(error.value.__cause__, RuntimeError)
