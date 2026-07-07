"""Daily action log 생성 실행기.

Airflow DAG은 이 모듈의 `run_daily_action_log`를 호출만 한다. 입력은 같은 날짜의
YouTube daily partition과 virtual user parquet이고, 출력은 action log dt partition이다.
"""
from __future__ import annotations

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
    generate_action_log_batch,
)
from autoresearch.action_logs.schema import EventGenerationRequest
from autoresearch.action_logs.video_source import load_video_records


_KST = ZoneInfo("Asia/Seoul")
_PARTITION_FILE = "part-0.parquet"
_QUARANTINE_FILE = "quarantine.jsonl"


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


def _copy_local_file(source: str | Path, destination: str, *, filesystem=None) -> None:
    """로컬 임시 파일을 local/GCS 최종 경로로 복사한다."""

    if filesystem is None:
        Path(destination).parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(source, destination)
        return

    with Path(source).open("rb") as src, filesystem.open_output_stream(destination) as dst:
        shutil.copyfileobj(src, dst)


def _build_generator(generator_name: str, model_name: str | None = None):
    """설정값에 따라 action log judgment generator를 만든다."""

    normalized = generator_name.strip().lower()
    if normalized in {"rule_based", "rule-based", "fixture"}:
        return RuleBasedActionLogGenerator()
    if normalized in {"openrouter", "llm"}:
        kwargs = {"model_name": model_name} if model_name else {}
        return OpenRouterActionLogGenerator(**kwargs)
    raise ValueError(
        "generator_name must be one of: rule_based, rule-based, fixture, openrouter, llm"
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
        request = EventGenerationRequest(
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
