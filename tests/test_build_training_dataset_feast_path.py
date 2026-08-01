"""build_training_dataset의 --assembly-source feast 경로 단위 테스트 (#358).

실제 feast/BigQuery 없이 glue만 검증한다: 인자 검증, spine→조회→CSV 컬럼 선택,
누락 피처 가드. feast 조회 자체는 tests/test_feast_retrieval_integration_feast.py가
실물(로컬 File store)로 검증한다.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

import pandas as pd
import pytest

from src.features import feast_retrieval
from src.features.model_contract import MODEL_FEATURE_COLUMNS
from src.pipeline import build_training_dataset as btd
from src.pipeline.training_provenance import (
    ProvenanceValidationError,
    RegistryProvenance,
    load_training_snapshot_manifest,
    snapshot_manifest_path,
)


def test_main_requires_event_dates() -> None:
    # C2 feast-only: 기간(events_start_date/events_end_date) 없이는 조립할 spine을 못 정해 실패.
    with pytest.raises(ValueError, match="events_start_date/events_end_date"):
        btd.main()


def test_apply_cold_start_defaults_fills_nulls_serving_rule() -> None:
    # 영상 미발견 등으로 null인 피처를 서빙과 같은 규칙(카테고리→'unknown', 수치→0)으로 채운다.
    df = pd.DataFrame(
        {
            "category_id": ["Gaming", None],  # categorical
            "view_count": [100, None],  # 수치
            "topic_similarity": [0.9, None],  # 수치(float)
            "clicked": [1, 0],  # 모델 피처 아님 → 안 건드림
        }
    )
    out = feast_retrieval.apply_cold_start_defaults(df)
    assert out.loc[1, "category_id"] == "unknown"
    assert out.loc[1, "view_count"] == 0
    assert out.loc[1, "topic_similarity"] == 0.0
    # 비-null과 비-모델 컬럼은 보존.
    assert out.loc[0, "category_id"] == "Gaming"
    assert out.loc[0, "topic_similarity"] == 0.9
    assert out["clicked"].tolist() == [1, 0]


def test_drop_user_dynamic_gap_rows() -> None:
    # UserDynamic 피처가 **전부** null인 행만 드롭(#357 (C) 결손 가시화). 일부만 null이거나
    # 값이 있으면 유지하고, 영상 피처 null(category_id)은 이 판정과 무관.
    df = pd.DataFrame(
        {
            "recent_click_count_7d": [5, None, None],
            "recent_view_count_7d": [3, None, None],
            "recent_watch_time_7d": [10, None, None],
            "recent_like_count_7d": [0, None, None],
            "historical_category_affinity": ["Gaming", None, "Music"],
            "total_event_count_7d": [8, None, None],
            "category_id": ["Gaming", None, "Music"],  # 영상 null은 gap 판정과 무관
            "clicked": [1, 1, 0],
        }
    )
    out = feast_retrieval.drop_user_dynamic_gap_rows(df)
    # row1(전 UserDynamic null) 드롭 / row0(값 있음)·row2(affinity 있음) 유지.
    assert len(out) == 2
    assert out["clicked"].tolist() == [1, 0]


def _fake_env(monkeypatch, features: pd.DataFrame) -> None:
    spine = pd.DataFrame(
        [{"user_id": "u1", "video_id": "v1",
          "event_timestamp": pd.Timestamp("2026-07-02", tz="UTC"), "clicked": 1}]
    )
    monkeypatch.setenv("GCS_REGISTRY_PATH", "gs://fake/registry.db")
    monkeypatch.setenv("GCS_STAGING_LOCATION", "gs://fake/staging/")
    monkeypatch.setattr(btd, "load_training_entity_spine", lambda s, e: spine)
    def fake_download(uri: str, destination: Path) -> RegistryProvenance:
        destination.write_bytes(b"registry-v1")
        return RegistryProvenance(
            uri=uri,
            generation="1",
            sha256=hashlib.sha256(b"registry-v1").hexdigest(),
        )

    monkeypatch.setattr(btd, "_download_pinned_registry", fake_download)
    monkeypatch.setattr(feast_retrieval, "build_offline_feature_store", lambda *a, **k: object())
    monkeypatch.setattr(feast_retrieval, "retrieve_training_features", lambda store, sp: features)


def test_assemble_pins_registry_and_writes_snapshot(tmp_path, monkeypatch) -> None:
    features = pd.DataFrame([{c: 0 for c in MODEL_FEATURE_COLUMNS}])
    features["clicked"] = 1
    _fake_env(monkeypatch, features)
    seen: dict[str, str] = {}

    def fake_download(uri: str, destination: Path) -> RegistryProvenance:
        destination.write_bytes(b"registry-v7")
        return RegistryProvenance(
            uri=uri,
            generation="7",
            sha256=hashlib.sha256(b"registry-v7").hexdigest(),
        )

    def fake_store(registry_path: str, **kwargs: object) -> object:
        seen["registry_path"] = registry_path
        return object()

    monkeypatch.setattr(btd, "_download_pinned_registry", fake_download)
    monkeypatch.setattr(feast_retrieval, "build_offline_feature_store", fake_store)

    output_path = tmp_path / "out.csv"
    btd._assemble_via_feast(str(output_path), "2026-07-01", "2026-07-30")

    assert seen["registry_path"].endswith("registry.db")
    assert not seen["registry_path"].startswith("gs://")
    manifest = load_training_snapshot_manifest(output_path)
    assert manifest.registry_generation == "7"
    assert manifest.registry_sha256 == hashlib.sha256(b"registry-v7").hexdigest()
    assert snapshot_manifest_path(output_path).is_file()


def test_registry_download_failure_creates_no_dataset_or_sidecar(tmp_path, monkeypatch) -> None:
    features = pd.DataFrame([{c: 0 for c in MODEL_FEATURE_COLUMNS}])
    features["clicked"] = 1
    _fake_env(monkeypatch, features)

    def fail_download(*args: object, **kwargs: object) -> RegistryProvenance:
        raise ProvenanceValidationError("registry download failed")

    monkeypatch.setattr(btd, "_download_pinned_registry", fail_download)
    output_path = tmp_path / "out.csv"

    with pytest.raises(ProvenanceValidationError, match="registry"):
        btd._assemble_via_feast(str(output_path), "2026-07-01", "2026-07-30")

    assert not output_path.exists()
    assert not snapshot_manifest_path(output_path).exists()


class _FakeBlob:
    def __init__(self, *, generation: int | None, payload: bytes = b"registry-v7") -> None:
        self.generation = generation
        self.payload = payload
        self.reload_called = False

    def reload(self) -> None:
        self.reload_called = True

    def download_to_filename(self, filename: str) -> None:
        Path(filename).write_bytes(self.payload)


class _FakeBucket:
    def __init__(self, metadata_blob: _FakeBlob, pinned_blob: _FakeBlob) -> None:
        self.metadata_blob = metadata_blob
        self.pinned_blob = pinned_blob
        self.calls: list[tuple[str, int | None]] = []

    def blob(self, name: str, generation: int | None = None) -> _FakeBlob:
        self.calls.append((name, generation))
        return self.metadata_blob if generation is None else self.pinned_blob


class _FakeStorageClient:
    def __init__(self, bucket: _FakeBucket) -> None:
        self._bucket = bucket

    def bucket(self, name: str) -> _FakeBucket:
        assert name == "bucket"
        return self._bucket


def test_download_pinned_registry_uses_metadata_generation(tmp_path) -> None:
    metadata_blob = _FakeBlob(generation=7)
    pinned_blob = _FakeBlob(generation=7, payload=b"exact-registry")
    bucket = _FakeBucket(metadata_blob, pinned_blob)
    client = _FakeStorageClient(bucket)
    destination = tmp_path / "registry.db"

    provenance = btd._download_pinned_registry(
        "gs://bucket/path/registry.db", destination, client=client
    )

    assert metadata_blob.reload_called is True
    assert bucket.calls == [("path/registry.db", None), ("path/registry.db", 7)]
    assert destination.read_bytes() == b"exact-registry"
    assert provenance.uri == "gs://bucket/path/registry.db"
    assert provenance.generation == "7"
    assert provenance.sha256 == hashlib.sha256(b"exact-registry").hexdigest()


def test_download_pinned_registry_rejects_non_gs_uri(tmp_path) -> None:
    with pytest.raises(ProvenanceValidationError, match="gs://"):
        btd._download_pinned_registry("https://bucket/registry.db", tmp_path / "registry.db")


def test_download_pinned_registry_rejects_missing_generation(tmp_path) -> None:
    metadata_blob = _FakeBlob(generation=None)
    bucket = _FakeBucket(metadata_blob, _FakeBlob(generation=None))

    with pytest.raises(ProvenanceValidationError, match="generation"):
        btd._download_pinned_registry(
            "gs://bucket/registry.db",
            tmp_path / "registry.db",
            client=_FakeStorageClient(bucket),
        )


def test_assemble_via_feast_writes_contract_columns(tmp_path, monkeypatch) -> None:
    features = pd.DataFrame([{c: 0 for c in MODEL_FEATURE_COLUMNS}])
    features["clicked"] = 1
    features["user_id"] = "u1"  # 여분 컬럼은 버려져야 한다
    _fake_env(monkeypatch, features)

    out_path = str(tmp_path / "out.csv")
    # 이 테스트가 보는 건 컬럼 계약이지 커버리지가 아니다 — 1행 mock spine이
    # 커버리지 가드(#464)에 걸리지 않도록 명시적으로 우회한다.
    btd._assemble_via_feast(out_path, "2026-07-07", "2026-07-21", min_coverage_days=0)

    written = pd.read_csv(out_path)
    # 정확히 21피처 + clicked, 순서도 계약대로.
    assert list(written.columns) == [*MODEL_FEATURE_COLUMNS, "clicked"]
    assert len(written) == 1
    assert int(written["clicked"].iloc[0]) == 1


def test_assemble_via_feast_empty_warns_and_reports_counts(
    tmp_path, monkeypatch, capsys
) -> None:
    # 관측성(#359 C2 리뷰): UserDynamic 전량 결손(#365)으로 전 행이 gap 드롭돼 학습 0행이면
    # 조용히 성공하지 않고 조회->드롭->학습 행 수 + 경고를 stdout에 남긴다.
    row = {c: 0 for c in MODEL_FEATURE_COLUMNS}
    for c in feast_retrieval._USER_DYNAMIC_COLUMNS:
        row[c] = None  # 전 UserDynamic null → gap 드롭 대상
    features = pd.DataFrame([row])
    features["clicked"] = 1
    _fake_env(monkeypatch, features)

    out_path = str(tmp_path / "out.csv")
    btd._assemble_via_feast(out_path, "2026-07-07", "2026-07-21", min_coverage_days=0)

    written = pd.read_csv(out_path)
    assert len(written) == 0  # 전량 드롭
    out = capsys.readouterr().out
    assert "조회 1행" in out and "드롭 1행" in out and "학습 0행" in out
    assert "[경고]" in out


def test_assemble_via_feast_missing_feature_raises(tmp_path, monkeypatch) -> None:
    # 조회 결과에 모델 피처가 빠지면 조용히 넘기지 않고 즉시 실패.
    features = pd.DataFrame([{"category_id": "Gaming", "clicked": 1}])
    _fake_env(monkeypatch, features)
    with pytest.raises(ValueError, match="누락된 모델 피처"):
        btd._assemble_via_feast(
            str(tmp_path / "out.csv"), "2026-07-07", "2026-07-21", min_coverage_days=0
        )


def test_assemble_via_feast_guard_fires_before_expensive_retrieval(
    tmp_path, monkeypatch
) -> None:
    """가드가 조립 안에서 실제로 발동하고, **비싼 조회 전에** 멈추는지 고정한다(#464).

    순수 함수 테스트(test_spine_coverage_guard.py)와 별개로 **배선**을 본다. 검증 호출이
    나중에 retrieve_training_features 아래로 옮겨져도 이 테스트가 잡는다 — 그것이 이 PR이
    내세운 "실패할 조립에 BigQuery 스캔을 쓰지 않는다"는 근거이기 때문이다.
    """
    called = {"store": 0, "retrieve": 0}
    features = pd.DataFrame([{c: 0 for c in MODEL_FEATURE_COLUMNS}])
    features["clicked"] = 1
    _fake_env(monkeypatch, features)

    def _fail_if_called(name):
        def _inner(*args, **kwargs):
            called[name] += 1
            return object() if name == "store" else features

        return _inner

    monkeypatch.setattr(
        feast_retrieval, "build_offline_feature_store", _fail_if_called("store")
    )
    monkeypatch.setattr(
        feast_retrieval, "retrieve_training_features", _fail_if_called("retrieve")
    )

    # _fake_env의 spine은 2026-07-02 하루뿐 — 기본 기준(3일)에 미달한다.
    with pytest.raises(ValueError, match="학습에 쓸 수 있는 날이 부족합니다"):
        btd._assemble_via_feast(str(tmp_path / "out.csv"), "2026-07-01", "2026-07-07")

    assert called == {"store": 0, "retrieve": 0}


def test_assemble_via_feast_returns_coverage_for_lineage(tmp_path, monkeypatch) -> None:
    # 조립이 실측 커버리지를 돌려줘야 run-pipeline이 MLflow lineage에 남길 수 있다(#464 리뷰).
    features = pd.DataFrame([{c: 0 for c in MODEL_FEATURE_COLUMNS}])
    features["clicked"] = 1
    _fake_env(monkeypatch, features)

    coverage = btd._assemble_via_feast(
        str(tmp_path / "out.csv"), "2026-07-01", "2026-07-03", min_coverage_days=0
    )

    assert coverage.requested_days == ("2026-07-01", "2026-07-02", "2026-07-03")
    # 1행짜리 mock spine이라 정상일로 세지 않는다 — 그 사실이 그대로 lineage에 남아야 한다.
    assert coverage.usable_days == ()
    assert coverage.sparse_days == ("2026-07-02",)
    params = coverage.as_lineage_params(min_days=0)
    assert params["spine_coverage_guard"] == "off"
    assert params["spine_requested_days"] == "3"
