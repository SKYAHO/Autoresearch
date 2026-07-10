"""Daily action log 생성 실행기.

Airflow DAG은 이 모듈의 `run_daily_action_log`를 호출만 한다. 입력은 같은 날짜의
YouTube daily partition과 virtual user parquet이고, 출력은 action log dt partition이다.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import tempfile
from datetime import UTC, date, datetime, time, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pyarrow.parquet as pq

from autoresearch.action_logs.llm_generator import (
    OpenRouterActionLogGenerator,
    RuleBasedActionLogGenerator,
)
from autoresearch.action_logs.pipeline import (
    ActionLogGenerationError,
    expand_action_log_drafts,
    generate_action_log_batch,
    generate_action_log_drafts,
    read_action_log_draft_parquet,
    write_action_log_draft_parquet,
    write_event_log_parquet,
    write_quarantine_jsonl,
)
from autoresearch.action_logs.schema import (
    ACTION_LOG_SCHEMA_VERSION,
    PROMPT_VERSION,
    ActionLogShardManifest,
    EventGenerationRequest,
    QuarantineRecord,
)
from autoresearch.action_logs.video_source import load_video_records


_KST = ZoneInfo("Asia/Seoul")
_PARTITION_FILE = "part-0.parquet"
_QUARANTINE_FILE = "quarantine.jsonl"
_MANIFEST_FILE = "manifest.json"


def _strip_gs(path: str) -> str:
    """pyarrow GcsFileSystem용으로 gs:// prefix를 제거한다."""

    return path[5:] if path.startswith("gs://") else path


def _dt_path(
    base_path: str,
    partition_date: date,
    filename: str,
    *,
    filesystem=None,
) -> str:
    """local/GCS 공통 dt partition 파일 경로를 만든다."""

    if filesystem is None:
        return str(Path(base_path) / f"dt={partition_date:%Y-%m-%d}" / filename)
    return f"{_strip_gs(base_path).rstrip('/')}/dt={partition_date:%Y-%m-%d}/{filename}"


def _dt_shard_path(
    base_path: str,
    partition_date: date,
    shard_index: int,
    filename: str,
    *,
    filesystem=None,
) -> str:
    """local/GCS 공통 dt partition shard 파일 경로를 만든다."""

    shard_dir = f"shard={shard_index:03d}"
    if filesystem is None:
        return str(
            Path(base_path)
            / f"dt={partition_date:%Y-%m-%d}"
            / shard_dir
            / filename
        )
    return (
        f"{_strip_gs(base_path).rstrip('/')}/dt={partition_date:%Y-%m-%d}/"
        f"{shard_dir}/{filename}"
    )


def _input_path(path: str, *, filesystem=None) -> str:
    """filesystem 주입 여부에 맞춰 입력 경로를 정규화한다."""

    return _strip_gs(path) if filesystem is not None else path


def _read_virtual_users(path: str, *, filesystem=None) -> list[dict]:
    """virtual user parquet을 읽어 action log 파이프라인 입력 dict 목록으로 반환한다."""

    return pq.read_table(_input_path(path, filesystem=filesystem), filesystem=filesystem).to_pylist()


def _write_table(table, path: str, *, filesystem=None) -> None:
    """pyarrow Table을 local 또는 주입된 filesystem 경로에 쓴다."""

    if filesystem is None:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    pq.write_table(table, path, filesystem=filesystem)


def _write_json_file(payload: dict[str, object], path: str, *, filesystem=None) -> None:
    """local 또는 주입된 filesystem에 JSON 파일을 쓴다."""

    data = json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")
    if filesystem is None:
        file_path = Path(path)
        file_path.parent.mkdir(parents=True, exist_ok=True)
        file_path.write_bytes(data)
        return

    with filesystem.open_output_stream(path) as file:
        file.write(data)


def _read_json_file(path: str, *, filesystem=None) -> dict[str, object]:
    """local 또는 주입된 filesystem의 JSON object를 읽는다."""

    if filesystem is None:
        data = Path(path).read_bytes()
    else:
        with filesystem.open_input_file(path) as file:
            data = file.read()
    payload = json.loads(data)
    if not isinstance(payload, dict):
        raise ValueError(f"JSON object expected: {path}")
    return payload


def _copy_local_file(source: str | Path, destination: str, *, filesystem=None) -> None:
    """로컬 임시 파일을 local/GCS 최종 경로로 복사한다."""

    if filesystem is None:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return

    with Path(source).open("rb") as src, filesystem.open_output_stream(destination) as dst:
        shutil.copyfileobj(src, dst)


def _read_quarantine_jsonl(path: str, *, filesystem=None) -> list[QuarantineRecord]:
    """local/GCS quarantine JSONL 파일을 읽어 QuarantineRecord 목록으로 반환한다."""

    if filesystem is None:
        file_path = Path(path)
        if not file_path.exists():
            return []
        lines = file_path.read_text(encoding="utf-8").splitlines()
    else:
        try:
            with filesystem.open_input_file(path) as file:
                lines = file.read().decode("utf-8").splitlines()
        except FileNotFoundError:
            return []

    return [
        QuarantineRecord.model_validate(json.loads(line))
        for line in lines
        if line.strip()
    ]


def _select_virtual_user_shard(
    virtual_users: list[dict],
    shard_index: int,
    shard_count: int,
) -> list[dict]:
    """virtual user parquet 순서를 유지하는 contiguous shard slice를 반환한다."""

    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")
    if not 0 <= shard_index < shard_count:
        raise ValueError(
            f"shard_index must satisfy 0 <= shard_index < shard_count "
            f"(shard_index={shard_index}, shard_count={shard_count})"
        )

    start = len(virtual_users) * shard_index // shard_count
    end = len(virtual_users) * (shard_index + 1) // shard_count
    return virtual_users[start:end]


def _normalize_generator_name(generator_name: str) -> str:
    """generator alias를 manifest에 기록할 canonical 이름으로 정규화한다."""

    normalized = generator_name.strip().lower()
    if normalized in {"rule_based", "rule-based", "fixture"}:
        return "rule_based"
    if normalized in {"openrouter", "llm"}:
        return "openrouter"
    raise ValueError(
        "generator_name must be one of: rule_based, rule-based, fixture, openrouter, llm"
    )


def _build_generator(generator_name: str, model_name: str | None = None):
    """설정값에 따라 action log judgment generator를 만든다."""

    normalized = _normalize_generator_name(generator_name)
    if normalized == "rule_based":
        return RuleBasedActionLogGenerator()
    if normalized == "openrouter":
        kwargs = {"model_name": model_name} if model_name else {}
        return OpenRouterActionLogGenerator(**kwargs)
    raise AssertionError(f"unsupported normalized generator: {normalized}")


def _fingerprint_payload(
    *,
    generator_name: str,
    model_name: str,
    request: EventGenerationRequest,
) -> dict[str, object]:
    """출력 재현성에 영향을 주는 설정만 canonical fingerprint 입력으로 만든다."""

    history_end = request.history_end
    if history_end.tzinfo is None:
        history_end = history_end.replace(tzinfo=UTC)
    return {
        "generator": _normalize_generator_name(generator_name),
        "model_name": model_name,
        "candidates_per_user": request.candidates_per_user,
        "target_ctr": request.target_ctr,
        "personalized_ratio": request.personalized_ratio,
        "popular_ratio": request.popular_ratio,
        "exploration_ratio": request.exploration_ratio,
        "seed": request.seed,
        "chunk_size": request.chunk_size,
        "history_days": request.history_days,
        "history_end": history_end.astimezone(UTC).isoformat(),
        "max_events_per_user_per_day": request.max_events_per_user_per_day,
        "schema_version": ACTION_LOG_SCHEMA_VERSION,
        "prompt_version": PROMPT_VERSION,
    }


def _config_fingerprint(
    *,
    generator_name: str,
    model_name: str,
    request: EventGenerationRequest,
) -> str:
    """재현성 설정의 SHA-256 fingerprint를 반환한다."""

    payload = _fingerprint_payload(
        generator_name=generator_name,
        model_name=model_name,
        request=request,
    )
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
    return hashlib.sha256(encoded).hexdigest()


def _manifest_request(manifest: ActionLogShardManifest, tmp_dir: Path) -> EventGenerationRequest:
    """검증된 shard manifest에서 merge용 요청 계약을 복원한다."""

    return _build_request(
        partition_date=manifest.partition_date,
        tmp_dir=tmp_dir,
        candidates_per_user=manifest.candidates_per_user,
        target_ctr=manifest.target_ctr,
        personalized_ratio=manifest.personalized_ratio,
        popular_ratio=manifest.popular_ratio,
        exploration_ratio=manifest.exploration_ratio,
        seed=manifest.seed,
        max_concurrency=1,
        chunk_size=manifest.chunk_size,
        max_quarantine_ratio=manifest.max_quarantine_ratio,
        history_end=manifest.history_end,
    )


def _default_history_end(partition_date: date) -> datetime:
    """partition_date 하루 안에 timestamp가 배치되도록 KST 다음날 00:00을 끝으로 둔다."""

    end_kst = datetime.combine(
        partition_date + timedelta(days=1),
        time.min,
        tzinfo=_KST,
    )
    return end_kst.astimezone(UTC)


def _validate_event_partition_dates(events, partition_date: date) -> None:
    """모든 event_timestamp가 출력 dt partition의 KST 날짜 안에 있는지 검증한다."""

    for event in events:
        timestamp = event.event_timestamp
        if timestamp.tzinfo is None:
            timestamp = timestamp.replace(tzinfo=UTC)
        event_date = timestamp.astimezone(_KST).date()
        if event_date != partition_date:
            raise ValueError(
                "event_timestamp outside partition_date "
                f"(event_id={event.event_id}, event_date={event_date}, "
                f"partition_date={partition_date})"
            )


def _build_request(
    *,
    partition_date: date,
    tmp_dir: Path,
    candidates_per_user: int,
    target_ctr: float,
    personalized_ratio: float,
    popular_ratio: float,
    exploration_ratio: float,
    seed: int,
    max_concurrency: int,
    chunk_size: int,
    max_quarantine_ratio: float,
    history_end: datetime | None,
) -> EventGenerationRequest:
    """daily runner의 공통 EventGenerationRequest를 만든다."""

    return EventGenerationRequest(
        target_ctr=target_ctr,
        candidates_per_user=candidates_per_user,
        personalized_ratio=personalized_ratio,
        popular_ratio=popular_ratio,
        exploration_ratio=exploration_ratio,
        history_days=1,
        history_end=history_end or _default_history_end(partition_date),
        max_events_per_user_per_day=candidates_per_user,
        seed=seed,
        max_concurrency=max_concurrency,
        chunk_size=chunk_size,
        max_quarantine_ratio=max_quarantine_ratio,
        output_path=str(tmp_dir / "event_log.parquet"),
        warehouse_output_path=str(tmp_dir / "event_log.jsonl"),
        quarantine_output_path=str(tmp_dir / "quarantine.jsonl"),
    )


def run_daily_action_log(
    *,
    partition_date: date,
    youtube_base_path: str,
    virtual_users_path: str,
    output_base_path: str,
    quarantine_base_path: str | None = None,
    filesystem=None,
    candidates_per_user: int = 24,
    target_ctr: float = 0.02,
    personalized_ratio: float = 0.7,
    popular_ratio: float = 0.2,
    exploration_ratio: float = 0.1,
    seed: int = 42,
    max_concurrency: int = 1,
    chunk_size: int = 0,
    max_quarantine_ratio: float = 0.5,
    generator_name: str = "rule_based",
    model_name: str | None = None,
    history_end: datetime | None = None,
) -> dict[str, object]:
    """하루치 YouTube partition과 virtual user parquet으로 action log를 생성한다.

    Args:
        partition_date: 처리할 dt 날짜.
        youtube_base_path: `.../data_lake/youtube_trending_kr` 루트.
        virtual_users_path: virtual user parquet 경로.
        output_base_path: `.../data_lake/action_log` 출력 루트.
        quarantine_base_path: quarantine jsonl 출력 루트. None이면 최종 복사를 생략한다.
        filesystem: None(로컬) 또는 pyarrow filesystem(GCS 등).
    """

    youtube_path = _dt_path(
        youtube_base_path,
        partition_date,
        _PARTITION_FILE,
        filesystem=filesystem,
    )
    output_path = _dt_path(
        output_base_path,
        partition_date,
        _PARTITION_FILE,
        filesystem=filesystem,
    )
    quarantine_path = (
        _dt_path(
            quarantine_base_path,
            partition_date,
            _QUARANTINE_FILE,
            filesystem=filesystem,
        )
        if quarantine_base_path
        else ""
    )

    videos = load_video_records(youtube_path, filesystem=filesystem)
    virtual_users = _read_virtual_users(virtual_users_path, filesystem=filesystem)
    generator = _build_generator(generator_name, model_name)

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        request = _build_request(
            partition_date=partition_date,
            tmp_dir=tmp_dir,
            candidates_per_user=candidates_per_user,
            target_ctr=target_ctr,
            personalized_ratio=personalized_ratio,
            popular_ratio=popular_ratio,
            exploration_ratio=exploration_ratio,
            seed=seed,
            max_concurrency=max_concurrency,
            chunk_size=chunk_size,
            max_quarantine_ratio=max_quarantine_ratio,
            history_end=history_end,
        )
        try:
            result = generate_action_log_batch(
                request,
                virtual_users,
                videos,
                generator,
            )
        except ActionLogGenerationError:
            quarantine_file = tmp_dir / "quarantine.jsonl"
            if quarantine_path and quarantine_file.exists():
                _copy_local_file(quarantine_file, quarantine_path, filesystem=filesystem)
            raise

        _validate_event_partition_dates(result.batch.events, partition_date)
        event_table = pq.read_table(request.output_path)
        _write_table(event_table, output_path, filesystem=filesystem)
        if quarantine_path:
            _copy_local_file(
                request.quarantine_output_path,
                quarantine_path,
                filesystem=filesystem,
            )

    return {
        **result.summary,
        "partition_date": f"{partition_date:%Y-%m-%d}",
        "users": len(virtual_users),
        "videos": len(videos),
        "output_path": output_path,
        "quarantine_path": quarantine_path,
    }


def run_daily_action_log_shard(
    *,
    partition_date: date,
    shard_index: int,
    shard_count: int,
    youtube_base_path: str,
    virtual_users_path: str,
    output_base_path: str,
    quarantine_base_path: str | None = None,
    filesystem=None,
    candidates_per_user: int = 24,
    target_ctr: float = 0.02,
    personalized_ratio: float = 0.7,
    popular_ratio: float = 0.2,
    exploration_ratio: float = 0.1,
    seed: int = 42,
    max_concurrency: int = 1,
    chunk_size: int = 0,
    max_quarantine_ratio: float = 0.5,
    generator_name: str = "rule_based",
    model_name: str | None = None,
    history_end: datetime | None = None,
) -> dict[str, object]:
    """하루치 action log 생성을 위한 shard work parquet을 생성한다.

    Shard output은 최종 EventLog가 아니라 `ImpressionDraft` parquet이다. 최종
    CTR 정규화와 event_id 부여는 `merge_daily_action_log_shards`에서 한 번만
    수행한다.
    """

    youtube_path = _dt_path(
        youtube_base_path,
        partition_date,
        _PARTITION_FILE,
        filesystem=filesystem,
    )
    output_path = _dt_shard_path(
        output_base_path,
        partition_date,
        shard_index,
        _PARTITION_FILE,
        filesystem=filesystem,
    )
    manifest_path = _dt_shard_path(
        output_base_path,
        partition_date,
        shard_index,
        _MANIFEST_FILE,
        filesystem=filesystem,
    )
    quarantine_path = (
        _dt_shard_path(
            quarantine_base_path,
            partition_date,
            shard_index,
            _QUARANTINE_FILE,
            filesystem=filesystem,
        )
        if quarantine_base_path
        else ""
    )

    videos = load_video_records(youtube_path, filesystem=filesystem)
    virtual_users = _read_virtual_users(virtual_users_path, filesystem=filesystem)
    shard_users = _select_virtual_user_shard(
        virtual_users,
        shard_index,
        shard_count,
    )
    generator = _build_generator(generator_name, model_name)
    resolved_model_name = str(generator.model_name).strip()
    if not resolved_model_name:
        raise ValueError("generator model_name must not be empty")

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        request = _build_request(
            partition_date=partition_date,
            tmp_dir=tmp_dir,
            candidates_per_user=candidates_per_user,
            target_ctr=target_ctr,
            personalized_ratio=personalized_ratio,
            popular_ratio=popular_ratio,
            exploration_ratio=exploration_ratio,
            seed=seed,
            max_concurrency=max_concurrency,
            chunk_size=chunk_size,
            max_quarantine_ratio=max_quarantine_ratio,
            history_end=history_end,
        )
        result = generate_action_log_drafts(
            request,
            shard_users,
            videos,
            generator,
            enforce_quarantine_limit=False,
        )

        draft_path = tmp_dir / "action_log_drafts.parquet"
        write_action_log_draft_parquet(result.drafts, draft_path)
        write_quarantine_jsonl(result.quarantine, request.quarantine_output_path)
        _copy_local_file(draft_path, output_path, filesystem=filesystem)
        if quarantine_path:
            _copy_local_file(
                request.quarantine_output_path,
                quarantine_path,
                filesystem=filesystem,
            )

        manifest = ActionLogShardManifest(
            partition_date=partition_date,
            shard_index=shard_index,
            shard_count=shard_count,
            generator=_normalize_generator_name(generator_name),
            model_name=resolved_model_name,
            candidates_per_user=request.candidates_per_user,
            target_ctr=request.target_ctr,
            personalized_ratio=request.personalized_ratio,
            popular_ratio=request.popular_ratio,
            exploration_ratio=request.exploration_ratio,
            seed=request.seed,
            chunk_size=request.chunk_size,
            max_quarantine_ratio=request.max_quarantine_ratio,
            history_end=request.history_end,
            total_work=result.total_work,
            completed_work=result.total_work,
            quarantine_count=len(result.quarantine),
            schema_version=ACTION_LOG_SCHEMA_VERSION,
            prompt_version=PROMPT_VERSION,
            config_fingerprint=_config_fingerprint(
                generator_name=generator_name,
                model_name=resolved_model_name,
                request=request,
            ),
        )
        _write_json_file(
            manifest.model_dump(mode="json"),
            manifest_path,
            filesystem=filesystem,
        )

    return {
        **result.summary,
        "partition_date": f"{partition_date:%Y-%m-%d}",
        "shard_index": shard_index,
        "shard_count": shard_count,
        "users_total": len(virtual_users),
        "users": len(shard_users),
        "videos": len(videos),
        "output_path": output_path,
        "quarantine_path": quarantine_path,
        "manifest_path": manifest_path,
        "config_fingerprint": manifest.config_fingerprint,
    }


def _load_shard_manifests(
    *,
    partition_date: date,
    shard_count: int,
    shard_output_base_path: str,
    filesystem=None,
) -> list[ActionLogShardManifest]:
    """모든 shard manifest를 읽고 병합 전 계약 일치를 검증한다."""

    manifests: list[ActionLogShardManifest] = []
    for shard_index in range(shard_count):
        manifest_path = _dt_shard_path(
            shard_output_base_path,
            partition_date,
            shard_index,
            _MANIFEST_FILE,
            filesystem=filesystem,
        )
        try:
            manifest = ActionLogShardManifest.model_validate(
                _read_json_file(manifest_path, filesystem=filesystem)
            )
        except FileNotFoundError as exc:
            raise ValueError(f"missing shard manifest: {manifest_path}") from exc
        if manifest.partition_date != partition_date:
            raise ValueError(
                "shard manifest partition_date mismatch "
                f"(shard={shard_index}, expected={partition_date}, "
                f"actual={manifest.partition_date})"
            )
        if manifest.shard_index != shard_index or manifest.shard_count != shard_count:
            raise ValueError(
                "shard manifest topology mismatch "
                f"(expected_index={shard_index}, actual_index={manifest.shard_index}, "
                f"expected_count={shard_count}, actual_count={manifest.shard_count})"
            )
        if manifest.schema_version != ACTION_LOG_SCHEMA_VERSION:
            raise ValueError(
                f"shard manifest schema_version mismatch: {manifest.schema_version}"
            )
        if manifest.prompt_version != PROMPT_VERSION:
            raise ValueError(
                f"shard manifest prompt_version mismatch: {manifest.prompt_version}"
            )
        request = _manifest_request(manifest, Path("."))
        expected_fingerprint = _config_fingerprint(
            generator_name=manifest.generator,
            model_name=manifest.model_name,
            request=request,
        )
        if manifest.config_fingerprint != expected_fingerprint:
            raise ValueError(
                "shard manifest config_fingerprint does not match its config "
                f"(shard={shard_index})"
            )
        manifests.append(manifest)

    fingerprints = {manifest.config_fingerprint for manifest in manifests}
    if len(fingerprints) != 1:
        raise ValueError("shard config_fingerprint mismatch")
    model_names = {manifest.model_name for manifest in manifests}
    if len(model_names) != 1:
        raise ValueError("shard model_name mismatch")
    quarantine_limits = {manifest.max_quarantine_ratio for manifest in manifests}
    if len(quarantine_limits) != 1:
        raise ValueError("shard max_quarantine_ratio mismatch")
    return manifests


def merge_daily_action_log_shards(
    *,
    partition_date: date,
    shard_count: int,
    shard_output_base_path: str,
    output_base_path: str,
    shard_quarantine_base_path: str | None = None,
    quarantine_base_path: str | None = None,
    filesystem=None,
    max_quarantine_ratio: float | None = None,
) -> dict[str, object]:
    """검증된 shard manifest 계약으로 최종 daily action log를 생성한다.

    shard 단계는 성공 draft를 보존하기 위해 quarantine 비율로 실패하지 않는다.
    merge 단계가 모든 manifest의 전체 work/quarantine을 합산해 전역 한도를
    단 한 번 검증한다.
    """

    if shard_count < 1:
        raise ValueError("shard_count must be at least 1")

    manifests = _load_shard_manifests(
        partition_date=partition_date,
        shard_count=shard_count,
        shard_output_base_path=shard_output_base_path,
        filesystem=filesystem,
    )
    contract = manifests[0]
    resolved_max_quarantine_ratio = contract.max_quarantine_ratio
    if (
        max_quarantine_ratio is not None
        and max_quarantine_ratio != resolved_max_quarantine_ratio
    ):
        raise ValueError(
            "merge max_quarantine_ratio does not match shard manifest "
            f"(merge={max_quarantine_ratio}, shard={resolved_max_quarantine_ratio})"
        )

    output_path = _dt_path(
        output_base_path,
        partition_date,
        _PARTITION_FILE,
        filesystem=filesystem,
    )
    quarantine_path = (
        _dt_path(
            quarantine_base_path,
            partition_date,
            _QUARANTINE_FILE,
            filesystem=filesystem,
        )
        if quarantine_base_path
        else ""
    )

    drafts = []
    quarantine: list[QuarantineRecord] = []
    for shard_index in range(shard_count):
        shard_path = _dt_shard_path(
            shard_output_base_path,
            partition_date,
            shard_index,
            _PARTITION_FILE,
            filesystem=filesystem,
        )
        drafts.extend(read_action_log_draft_parquet(shard_path, filesystem=filesystem))
        if shard_quarantine_base_path:
            shard_quarantine_path = _dt_shard_path(
                shard_quarantine_base_path,
                partition_date,
                shard_index,
                _QUARANTINE_FILE,
                filesystem=filesystem,
            )
            quarantine.extend(
                _read_quarantine_jsonl(shard_quarantine_path, filesystem=filesystem)
            )

    total_work = sum(manifest.total_work for manifest in manifests)
    quarantine_count = sum(manifest.quarantine_count for manifest in manifests)
    if shard_quarantine_base_path and len(quarantine) != quarantine_count:
        raise ValueError(
            "shard quarantine count does not match manifests "
            f"(records={len(quarantine)}, manifests={quarantine_count})"
        )
    quarantine_ratio = quarantine_count / total_work if total_work else 0.0
    if quarantine_ratio > resolved_max_quarantine_ratio:
        if quarantine_path and shard_quarantine_base_path:
            with tempfile.TemporaryDirectory() as tmp:
                quarantine_file = Path(tmp) / _QUARANTINE_FILE
                write_quarantine_jsonl(quarantine, quarantine_file)
                _copy_local_file(
                    quarantine_file,
                    quarantine_path,
                    filesystem=filesystem,
                )
        raise ActionLogGenerationError(
            f"global quarantine ratio {quarantine_ratio:.2f} exceeds "
            f"max_quarantine_ratio {resolved_max_quarantine_ratio:.2f} "
            f"(quarantined={quarantine_count}, total_work={total_work})"
        )

    with tempfile.TemporaryDirectory() as tmp:
        tmp_dir = Path(tmp)
        request = _manifest_request(contract, tmp_dir)
        result = expand_action_log_drafts(request, drafts, quarantine)
        _validate_event_partition_dates(result.batch.events, partition_date)
        write_event_log_parquet(result.batch, contract.model_name, request.output_path)
        _copy_local_file(request.output_path, output_path, filesystem=filesystem)
        if quarantine_path:
            write_quarantine_jsonl(quarantine, request.quarantine_output_path)
            _copy_local_file(
                request.quarantine_output_path,
                quarantine_path,
                filesystem=filesystem,
            )

    return {
        **result.summary,
        "quarantined_users": quarantine_count,
        "partition_date": f"{partition_date:%Y-%m-%d}",
        "shard_count": shard_count,
        "drafts": len(drafts),
        "total_work": total_work,
        "quarantine_count": quarantine_count,
        "config_fingerprint": contract.config_fingerprint,
        "model_name": contract.model_name,
        "output_path": output_path,
        "quarantine_path": quarantine_path,
    }
