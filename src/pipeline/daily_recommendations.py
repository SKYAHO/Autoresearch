"""일일 추천 결과 BQ 적재 배치.

champion 모델(models:/ctr-model@champion)로 일일 트렌딩 후보 전체를 가상 유저
전원에 대해 canonical 21개 피처로 채점해, 유저별 전체 순위를 user_recommendations 파티션 테이블에
멱등 적재한다. 비교 실험·노출 선정은 이 배치의 책임이 아니다(spec 참조).

피처 조립 소스는 `--assembly-source`로 고른다(#359): duckdb(기본, raw 재계산)와 feast(학습과
같은 offline store를 get_historical_features PIT로 조회 — 단일 as_of=candidate_dt+1로 영상·유저
스냅샷을 함께 조회). feast는 학습·시뮬레이션과 피처 출처를 통일해 train-serve skew를 없앤다.

spec: docs/specs/2026-07-21-daily-recommendations-batch.md,
      docs/specs/2026-07-13-public-batch-execution-contract.md (공개 batch 계약)
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
from datetime import UTC, date, datetime, timedelta
from typing import TYPE_CHECKING, Callable, Final, Sequence

import pandas as pd
from google.cloud import bigquery

if TYPE_CHECKING:
    from feast import FeatureStore

from autoresearch.jobs import BATCH_CONTRACT_VERSION
from src.features.feast_retrieval import build_pool_feature_frames_feast
from src.features.model_contract import require_model_feature_columns
from src.pipeline.build_training_dataset import (
    BIGQUERY_DATASET,
    BIGQUERY_PROJECT,
    derive_wide_events,
    feature_table_id,
    load_events_from_bigquery,
    raw_table_id,
)
from src.pipeline.virtual_user_adapter import to_personas_frame
from src.serving.model_loader import (
    RegistryModelSettings,
    ResolvedModel,
    load_reranker_with_lineage,
)
from src.serving.schemas import RerankedVideo
from src.pipeline.simulate_policy_round import _to_candidate_videos, build_pool_feature_frame

logger = logging.getLogger(__name__)
JOB_NAME: Final = "daily_recommendations"
_REVISION: Final = os.getenv("AUTORESEARCH_REVISION", "unknown")

_DURATION_PATTERN = re.compile(r"PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?")

RECOMMENDATIONS_SCHEMA: list[bigquery.SchemaField] = [
    bigquery.SchemaField("dt", "DATE"),
    bigquery.SchemaField("user_id", "STRING"),
    bigquery.SchemaField("video_id", "STRING"),
    bigquery.SchemaField("rank", "INTEGER"),
    bigquery.SchemaField("ctr_score", "FLOAT"),
    bigquery.SchemaField("model_run_id", "STRING"),
    bigquery.SchemaField("model_version", "STRING"),
    bigquery.SchemaField("events_dt", "DATE"),
    bigquery.SchemaField("generated_at", "TIMESTAMP"),
]


def parse_iso8601_duration(value: object) -> int:
    """ISO8601 duration 문자열(PT4M29S)을 초로 변환한다. 해석 불가 값은 0."""
    if not isinstance(value, str):
        return 0
    match = _DURATION_PATTERN.fullmatch(value)
    if not match:
        return 0
    hours, minutes, seconds = match.groups()
    return int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds or 0)


def to_recommendation_rows(
    user_id: str,
    ranked: list[RerankedVideo],
    *,
    dt: date,
    events_dt: date,
    model_run_id: str,
    model_version: str | None,
    generated_at: datetime,
) -> list[dict]:
    """채점 결과를 결정론적 순위 행으로 조립한다(동점은 video_id 오름차순)."""
    ordered = sorted(ranked, key=lambda item: (-item.ctr_score, item.video_id))
    return [
        {
            "dt": dt,
            "user_id": user_id,
            "video_id": item.video_id,
            "rank": position,
            "ctr_score": float(item.ctr_score),
            "model_run_id": model_run_id,
            "model_version": model_version,
            "events_dt": events_dt,
            "generated_at": generated_at,
        }
        for position, item in enumerate(ordered, start=1)
    ]


def ensure_output_table(client: bigquery.Client, table_id: str) -> None:
    """출력 테이블이 없으면 dt DAY 파티션 스키마로 생성한다(있으면 무시)."""
    table = bigquery.Table(table_id, schema=RECOMMENDATIONS_SCHEMA)
    table.time_partitioning = bigquery.TimePartitioning(
        type_=bigquery.TimePartitioningType.DAY, field="dt"
    )
    client.create_table(table, exists_ok=True)


def write_partition(
    client: bigquery.Client, table_id: str, frame: pd.DataFrame, dt: date
) -> None:
    """해당 날짜 파티션을 원자적으로 대체한다(재실행 멱등)."""
    destination = f"{table_id}${dt.strftime('%Y%m%d')}"
    job_config = bigquery.LoadJobConfig(
        schema=RECOMMENDATIONS_SCHEMA,
        write_disposition="WRITE_TRUNCATE",
    )
    client.load_table_from_dataframe(frame, destination, job_config=job_config).result()


def _required_tracking_uri() -> str:
    """registry 로드에 필요한 MLFLOW_TRACKING_URI를 읽는다."""
    import os

    value = os.getenv("MLFLOW_TRACKING_URI")
    if value is None or not value.strip():
        raise RuntimeError("MLFLOW_TRACKING_URI is required to load the champion model.")
    return value


def _default_registry_settings() -> RegistryModelSettings:
    """배치 기본 모델 설정 — models:/ctr-model@champion (env로 재정의 가능)."""
    import os

    return RegistryModelSettings(
        tracking_uri=_required_tracking_uri(),
        model_name=os.getenv("RERANK_REGISTRY_MODEL_NAME", "ctr-model"),
        alias=os.getenv("RERANK_REGISTRY_ALIAS", "champion"),
    )


def _max_partition_date(client: bigquery.Client, table_id: str) -> date:
    """테이블의 MAX(dt) 파티션 날짜를 조회한다."""
    query = f"SELECT MAX(dt) AS max_dt FROM `{table_id}`"
    row = next(iter(client.query(query).result()))
    if row.max_dt is None:
        raise RuntimeError(f"No partitions found in {table_id}")
    return row.max_dt


def _load_candidates(client: bigquery.Client, table_id: str, dt: date) -> pd.DataFrame:
    """후보 파티션을 학습 계약 컬럼명으로 조회하고 duration을 초로 정규화한다."""
    query = f"""
    SELECT video_id,
           video_category AS categoryId,
           video_duration AS duration,
           video_view_count AS viewCount,
           video_like_count AS likeCount,
           video_comment_count AS commentCount,
           channel_subscriber_count AS channelSubscriberCount,
           channel_view_count AS channelViewCount,
           channel_video_count AS channelVideoCount,
           video_published_at AS publishedAt
    FROM `{table_id}`
    WHERE dt = '{dt.isoformat()}'
    """
    frame = client.query(query).to_dataframe()
    frame["duration"] = frame["duration"].apply(parse_iso8601_duration)
    return frame


def _load_virtual_users(client: bigquery.Client, table_id: str) -> pd.DataFrame:
    """가상 유저 전원의 어댑터 입력 컬럼을 조회한다."""
    query = f"""
    SELECT user_id, age, occupation, hobby_keywords, interest_keywords, lifestyle_keywords,
           watch_time_band
    FROM `{table_id}`
    """
    return client.query(query).to_dataframe()


def _utc_now() -> datetime:
    return datetime.now(UTC)


def run_batch(
    *,
    candidate_dt: date | None = None,
    events_dt: date | None = None,
    max_users: int | None = None,
    output_table: str | None = None,
    dry_run: bool = False,
    max_skip_ratio: float = 0.1,
    bq_client: bigquery.Client | None = None,
    resolved: ResolvedModel | None = None,
    videos_raw: pd.DataFrame | None = None,
    personas: pd.DataFrame | None = None,
    events: pd.DataFrame | None = None,
    clock: Callable[[], datetime] = _utc_now,
    assembly_source: str = "duckdb",
    feature_store: FeatureStore | None = None,
) -> dict[str, object]:
    """일일 추천 배치를 실행하고 요약 리포트를 반환한다.

    bq_client·resolved·videos_raw·personas·events·clock은 테스트 주입용이며,
    None이면 실환경(BigQuery·MLflow registry)에서 로드한다.

    assembly_source: 모델 피처(21개) 조립 경로(#359). "feast"는 학습과 같은 offline PIT
    (``build_pool_feature_frame_feast``)를 써 train-serve skew를 없앤다. 이때 단일 as_of를
    쓰는데(offline PIT는 entity 행당 event_timestamp가 하나뿐), **candidate_dt+1**을 쓴다 —
    영상 PIT가 candidate_dt 트렌딩 스냅샷을(영상 나이 정확), 유저 PIT가 그 이하 최신
    UserDynamic(=events_dt 스냅샷, 60h ttl 안)을 골라, 기존 duckdb가 영상=candidate_dt/
    유저=events_dt로 분리하던 것을 단일 as_of로 흡수한다(A3-1 실측 확정). feast는
    ``feature_store`` 주입 또는 GCS_REGISTRY_PATH/GCS_STAGING_LOCATION env를 요구하고 feast
    파생 이미지에서 실행한다. "duckdb"(기본)는 raw 재계산으로 기존과 동일하며 #359에서 제거 예정.

    feast 모드 운영 전제(리뷰 #381):
    - **offline materialize 선행**: candidate_dt의 video/user 스냅샷이 배치 실행 전에 offline
      store에 적재돼 있어야 한다(VideoFeatureView ttl=None이라 미적재 시 stale 스냅샷 또는
      미발견→cold-start). daily는 DAG를 소유하지 않으므로 이 순서는 인접 저장소가 보장한다.
    - **ttl 창**: UserDynamic ttl=60h이므로 as_of=candidate_dt+1 기준 유효 스냅샷은 대략
      candidate_dt-1~candidate_dt 파티션이다. action log가 그보다 밀리면 전 유저 cold-start가
      되며, 그 비율을 리포트 ``cold_start_users``/``video_missing_rate``로 관측한다.
    - **events_dt 의미**: feast 모드에서 events_dt는 피처·as_of에 쓰이지 않고 ``user_recommendations``
      의 lineage 컬럼으로만 기록된다(duckdb 모드는 유저 이력 기준일). 소비자는 모드에 따라 의미가
      다름에 유의한다(공개 batch 계약 spec 참조).
    """
    import os

    if assembly_source not in ("duckdb", "feast"):
        raise ValueError(f"assembly_source must be 'duckdb' or 'feast': {assembly_source!r}")

    # dataset 계층 분리: raw(data_lake_*)는 CTR_TRAINING_BQ_RAW_DATASET,
    # feature/서빙 테이블은 기존 CTR_TRAINING_BQ_DATASET 으로 해석한다.
    trending_table = raw_table_id(
        os.getenv("CTR_TRAINING_BQ_VIDEOS_TABLE", "data_lake_youtube_trending_kr")
    )
    # CAVEAT(#232): asset_virtual_user_vu_1000 은 인프라 정리 작업에서 삭제될
    # 예정이다. 이 배치는 아직 Airflow DAG 으로 배포되지 않아 즉시 장애가 나지는
    # 않는다. GCS 원본 parquet(asset/virtual_user/vu_1000.parquet)이 여전히
    # source of truth 이며 scripts/load_raw_to_bigquery.py 로 재적재 가능하다.
    # virtual user 소스 확정은 후속 과제이므로 여기서는 dataset 을 옮기지 않고
    # 기존 feature dataset 해석을 그대로 유지한다.
    users_table = feature_table_id(
        os.getenv("CTR_TRAINING_BQ_VIRTUAL_USERS_TABLE", "asset_virtual_user_vu_1000")
    )
    output_table_id = feature_table_id(
        output_table
        or os.getenv("CTR_TRAINING_BQ_RECOMMENDATIONS_TABLE", "user_recommendations")
    )

    if resolved is None:
        resolved = load_reranker_with_lineage(_default_registry_settings())  # fail-fast
    reranker = resolved.reranker
    feature_columns = require_model_feature_columns(reranker.feature_columns)
    if bq_client is None:
        bq_client = bigquery.Client(project=BIGQUERY_PROJECT)

    if candidate_dt is None:
        candidate_dt = _max_partition_date(bq_client, trending_table)
    if events_dt is None:
        events_dt = _max_partition_date(
            bq_client,
            raw_table_id(
                os.getenv("CTR_TRAINING_BQ_ACTION_LOG_TABLE", "data_lake_action_log")
            ),
        )

    if videos_raw is None:
        videos_raw = _load_candidates(bq_client, trending_table, candidate_dt)
    if videos_raw.empty:
        raise RuntimeError(f"No candidates in partition dt={candidate_dt}")
    if personas is None:
        personas = to_personas_frame(_load_virtual_users(bq_client, users_table))
    if personas.empty:
        raise RuntimeError("No virtual users available for scoring")
    # feast 경로는 피처를 offline store에서 조회하므로 raw events 재계산이 불필요하다 —
    # duckdb 경로만 단일 파티션 events를 wide로 변환해 쓴다.
    if assembly_source == "duckdb" and events is None:
        # 단일 파티션 소비 계약: 파티션 간 UNION은 attribution·집계를 오염시킨다.
        iso = events_dt.isoformat()
        events = derive_wide_events(load_events_from_bigquery(iso, iso))

    user_ids = personas["uuid"].astype(str).tolist()
    if max_users is not None:
        user_ids = user_ids[:max_users]

    generated_at = clock()

    # 모델 피처(21개) 조립 경로 선택(#359). 바뀌는 건 유저별 pool 프레임을 만드는 방식뿐이고,
    # 이후 채점·순위·적재는 두 경로 공통이다.
    if assembly_source == "feast":
        if feature_store is None:
            import atexit
            import shutil
            import tempfile

            from src.features.feast_retrieval import build_offline_feature_store

            try:
                registry_path = os.environ["GCS_REGISTRY_PATH"]
                gcs_staging = os.environ["GCS_STAGING_LOCATION"]
            except KeyError as error:
                raise RuntimeError(
                    f"assembly_source='feast'는 환경변수 {error}가 필요합니다 "
                    "(offline 레지스트리·GCS staging 경로)"
                ) from error
            store_dir = tempfile.mkdtemp(prefix="feast_daily_")
            atexit.register(shutil.rmtree, store_dir, ignore_errors=True)
            feature_store = build_offline_feature_store(
                registry_path,
                project=BIGQUERY_PROJECT,
                dataset=BIGQUERY_DATASET,
                gcs_staging=gcs_staging,
                online_db_path=os.path.join(store_dir, "online.db"),
            )
        # 단일 as_of=candidate_dt+1(docstring): 영상은 candidate_dt 스냅샷, 유저는 그 이하 최신
        # UserDynamic을 offline PIT로 한 번에 조회. duckdb의 영상/유저 2기준 분리를 흡수한다.
        as_of = (candidate_dt + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        candidate_video_ids = [str(v) for v in videos_raw["video_id"].tolist()]
        # pool이 전 유저 공통이므로 (유저 × 후보) spine **1회 조회**로 전 유저 프레임을 배치 조립한다
        # (#359 C1, 리뷰 4: 유저마다 staged 2회 = 하루 ~2N BQ 잡 회피). registry/service 불일치 같은
        # 전 유저 공통 오류는 이 조회에서 fail-fast(유저 격리로 위장되지 않음). 서빙은 드롭 대신
        # cold-start로 채우므로(설계결정 3) 결손이 묻히는 걸 diagnostics 집계로 관측한다.
        feast_diag: dict = {"cold_start_users": 0, "video_missing": 0, "pool_total": 0}
        _frames = build_pool_feature_frames_feast(
            feature_store, user_ids, candidate_video_ids, as_of, diagnostics=feast_diag
        )

        def _pool_frame(user_id: str) -> pd.DataFrame:
            return _frames[user_id]
    else:
        feast_diag = None
        # events_dt 파티션 전체를 과거 이력으로 포함하되 이후 이벤트는 보지 않는다.
        as_of = (events_dt + timedelta(days=1)).strftime("%Y-%m-%d 00:00:00")
        # 영상 나이(days_since_upload)는 유저 이력 기준일이 아니라 추천 대상일 기준.
        snapshot_date = candidate_dt.isoformat()

        def _pool_frame(user_id: str) -> pd.DataFrame:
            return build_pool_feature_frame(
                personas=personas,
                events=events,
                videos_raw=videos_raw,
                user_id=user_id,
                as_of=as_of,
                snapshot_date=snapshot_date,
            )

    all_rows: list[dict] = []
    skipped: list[str] = []
    for user_id in user_ids:
        try:
            frame = _pool_frame(user_id)
            ranked = reranker.rerank(_to_candidate_videos(frame, feature_columns))
            all_rows.extend(
                to_recommendation_rows(
                    user_id,
                    ranked,
                    dt=candidate_dt,
                    events_dt=events_dt,
                    model_run_id=resolved.run_id,
                    model_version=resolved.model_version,
                    generated_at=generated_at,
                )
            )
        except Exception as error:  # noqa: BLE001 - spec가 유저 단위 격리를 요구하는 경계
            # 기본 핸들러(lastResort)는 extra를 렌더링하지 않으므로 진단 정보를
            # 메시지 본문에 포함한다(spec의 stderr 진단 계약).
            logger.warning(
                "daily recommendation user quarantined (user_id=%s, exception_type=%s)",
                user_id,
                type(error).__name__,
            )
            skipped.append(user_id)

    if user_ids and len(skipped) / len(user_ids) > max_skip_ratio:
        raise RuntimeError(
            f"Skip ratio {len(skipped)}/{len(user_ids)} exceeded {max_skip_ratio}; aborting without write."
        )
    if user_ids and not all_rows:
        # max_skip_ratio=1.0이어도 전량 실패 결과로 기존 파티션을 비우면 안 된다.
        raise RuntimeError(
            f"All {len(skipped)}/{len(user_ids)} users were skipped; aborting without write."
        )

    if not dry_run:
        ensure_output_table(bq_client, output_table_id)
        output_frame = pd.DataFrame(
            all_rows,
            columns=[field.name for field in RECOMMENDATIONS_SCHEMA],
        )
        write_partition(bq_client, output_table_id, output_frame, candidate_dt)

    report: dict[str, object] = {
        "event": "job_summary",
        "contract_version": BATCH_CONTRACT_VERSION,
        "job": JOB_NAME,
        "status": "succeeded",
        "dt": candidate_dt.isoformat(),
        "partition_date": candidate_dt.isoformat(),
        "events_dt": events_dt.isoformat(),
        "users": len(user_ids),
        "skipped_users": len(skipped),
        "rows": len(all_rows),
        "model_run_id": resolved.run_id,
        "model_version": resolved.model_version,
        "dry_run": dry_run,
    }
    # feast 관측: 개인화 결손(UserDynamic 전량 null 유저)·영상 미발견 비율을 리포트에 남긴다.
    # 서빙은 드롭 대신 채우므로(설계결정 3) skip 가드가 못 잡는 조용한 저하를 이 수치로 감지한다.
    if feast_diag is not None:
        scored = len(user_ids) - len(skipped)
        report["cold_start_users"] = feast_diag["cold_start_users"]
        report["video_missing_rate"] = (
            round(feast_diag["video_missing"] / feast_diag["pool_total"], 4)
            if feast_diag["pool_total"]
            else 0.0
        )
        # 채점 유저의 절반 넘게 UserDynamic 결손이면 materialize 지연/ttl 초과 신호 → 경고.
        if scored and feast_diag["cold_start_users"] / scored > 0.5:
            logger.warning(
                "feast personalization gap: %d/%d scored users had all-null UserDynamic "
                "(cold-start) — check feature_store materialization freshness / 60h ttl",
                feast_diag["cold_start_users"],
                scored,
            )
    return report


class BatchArgumentError(ValueError):
    """공개 batch 명령의 문법·범위 오류."""


class _ArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise BatchArgumentError(message)


def _iso_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as error:
        raise argparse.ArgumentTypeError("must be YYYY-MM-DD") from error


def _positive_int(value: str) -> int:
    parsed = int(value)
    if parsed < 1:
        raise argparse.ArgumentTypeError("must be at least 1")
    return parsed


def _skip_ratio(value: str) -> float:
    parsed = float(value)
    if not 0.0 <= parsed <= 1.0:
        raise argparse.ArgumentTypeError("must be between 0 and 1")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = _ArgumentParser(description="일일 추천 결과 BQ 적재 배치")
    parser.add_argument(
        "--version",
        action="version",
        version=json.dumps(
            {
                "application_revision": _REVISION,
                "contract_version": BATCH_CONTRACT_VERSION,
            },
            sort_keys=True,
        ),
    )
    parser.add_argument("--candidate-dt", type=_iso_date)
    parser.add_argument("--events-dt", type=_iso_date)
    parser.add_argument("--max-users", type=_positive_int)
    parser.add_argument("--output-table")
    parser.add_argument("--max-skip-ratio", type=_skip_ratio, default=0.1)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument(
        "--assembly-source",
        choices=["duckdb", "feast"],
        default="duckdb",
        help="모델 피처 조립 소스. feast=학습과 같은 offline PIT(#359, feast 파생 이미지 + "
        "GCS_REGISTRY_PATH/GCS_STAGING_LOCATION 필요), duckdb=raw 재계산(기본, #359에서 제거 예정)",
    )
    return parser


def _emit(payload: dict[str, object]) -> None:
    print(json.dumps(payload, ensure_ascii=False, sort_keys=True, default=str), flush=True)


def _failure_summary(error_type: str) -> dict[str, object]:
    return {
        "event": "job_summary",
        "contract_version": BATCH_CONTRACT_VERSION,
        "job": JOB_NAME,
        "status": "failed",
        "error_type": error_type,
    }


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 인자를 검증·실행하고 공개 종료 코드를 반환한다."""
    try:
        args = _build_parser().parse_args(argv)
    except BatchArgumentError:
        _emit(_failure_summary("invalid_arguments"))
        return 2

    try:
        report = run_batch(
            candidate_dt=args.candidate_dt,
            events_dt=args.events_dt,
            max_users=args.max_users,
            output_table=args.output_table,
            dry_run=args.dry_run,
            max_skip_ratio=args.max_skip_ratio,
            assembly_source=args.assembly_source,
        )
    except Exception as error:  # noqa: BLE001 - process boundary maps failures to exit 1
        logger.error("daily_recommendations failed (%s)", type(error).__name__)
        _emit(_failure_summary("runtime_failure"))
        return 1

    _emit(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
