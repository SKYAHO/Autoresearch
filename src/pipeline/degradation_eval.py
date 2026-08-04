"""모델 성능 열화 시점 측정 — 단일 cutoff 기반 forward degradation evaluation (#471).

[파이프라인] 학습/평가 구간 — cutoff 이전 데이터로 학습한 모델을 cutoff 이후 날짜에
하루 단위로 순차 적용해 일별 ROC-AUC를 측정하는 구간을 담당한다. 이슈·완료조건은
"rolling-origin evaluation"이라는 관용 표현을 쓰지만, 이 구현은 단일 cutoff만 다루는
1차 범위다 — 여러 cutoff를 간격을 두고 반복하는 다중 origin 확장은 다루지 않는다
(정본: ``docs/specs/2026-08-03-model-degradation-rolling-origin-evaluation.md`` §2, §10).

[기능] cutoff 기준 학습·평가 날짜 구간을 계약대로 계산하고(``training_window``,
``evaluation_dates``), 평가일 데이터셋을 상태(``EvaluationStatus``)로 분류한다
(``classify_evaluation_day``). ROC-AUC를 계산할 수 없는 날(결손·표본 부족·단일 클래스)을
조용히 건너뛰지 않고 상태로 남긴다. video feature staleness를 진단 전용 별도 조회로
측정하고(``resolve_video_feature_snapshot_timestamps``, ``compute_video_staleness_summary``),
"2개 연속 유효 관측치에서 degraded"인 첫 시점을 찾는다(``detect_degradation_point``).
``run_rolling_origin``이 cutoff 학습 1회 + 평가일 순회를 오케스트레이션하고, 각 실행의
산출물을 ``run_root`` 아래 조건·평가일별로 격리해 이전 실행을 덮어쓰지 않는다(fail-closed).

[비책임] 학습(``train.main``)·데이터 조립(``build_training_dataset.main``)·held-out
ROC-AUC 계산(``evaluate.evaluate_held_out_roc_auc``) 자체는 재구현하지 않고 그대로
호출한다. 승격 판정(``eligible``/``reject``/``hold``)은
``src/pipeline/experiment_evaluation.py``(#493 작업 중) 소유이며 이 모듈은 호출·수정하지
않는다. Plotly 시각화와 공개 CLI 명령은 각각 ``scripts/bench/``, ``src/cli.py``가 담당한다.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING

import pandas as pd
from pydantic import BaseModel, ConfigDict

from src.pipeline import build_training_dataset, train
from src.pipeline.evaluate import evaluate_held_out_roc_auc
from src.pipeline.training_provenance import (
    TrainingSnapshotManifest,
    load_training_snapshot_manifest,
    sha256_file,
    write_manifest_atomic,
)
from src.utils.model_utils import load_feature_columns, load_model

if TYPE_CHECKING:
    from collections.abc import Mapping, Sequence

    from google.cloud import bigquery

_DATE_FORMAT = "%Y-%m-%d"


class _ResultModel(BaseModel):
    """이 모듈의 결과 payload에 적용하는 불변 pydantic 기본 설정.

    ``training_provenance._ImmutableModel``과 같은 설정(extra 금지·frozen)을 쓰되,
    그 모듈이 다루는 학습 provenance 계약과는 별개 스키마라 여기서 별도로 둔다(#454
    `paired_experiment.py`의 `_ContractModel`과 같은 이유 — 각 모듈이 자기 결과 계약만
    소유한다).
    """

    model_config = ConfigDict(extra="forbid", frozen=True, allow_inf_nan=False)


def training_window(cutoff_date: str, window_days: int) -> tuple[str, str]:
    """cutoff 이전 학습 구간 [cutoff-W, cutoff)을 events_start/end_date로 환산한다.

    ``load_training_entity_spine``의 BigQuery 조회는 ``BETWEEN``(양끝 포함)이라, cutoff
    당일을 학습에서 제외하려면 종료일을 ``cutoff - 1일``로 당겨야 한다(spec §2.1).
    """
    cutoff = datetime.strptime(cutoff_date, _DATE_FORMAT)
    events_end_date = cutoff - timedelta(days=1)
    events_start_date = cutoff - timedelta(days=window_days)
    return events_start_date.strftime(_DATE_FORMAT), events_end_date.strftime(_DATE_FORMAT)


def evaluation_dates(cutoff_date: str, horizon_days: int) -> list[tuple[str, int]]:
    """평가 구간 [cutoff, cutoff+H)의 (날짜, elapsed_days) 목록을 만든다.

    cutoff 당일이 첫 평가일이다(elapsed_days=0, spec §2.1). elapsed_days는 관측 순번이
    아니라 cutoff 기준 달력 일수이며, 이 함수가 그 계약의 유일한 산출 지점이다 —
    결손일도 여기서 나온 elapsed_days를 그대로 쓴다(건너뛰거나 재번호하지 않는다).
    """
    cutoff = datetime.strptime(cutoff_date, _DATE_FORMAT)
    return [
        ((cutoff + timedelta(days=elapsed)).strftime(_DATE_FORMAT), elapsed)
        for elapsed in range(horizon_days)
    ]


class EvaluationStatus(str, Enum):
    """평가일 데이터셋의 분류 상태(spec §2.3)."""

    VALID = "valid"
    MISSING_DATE = "missing_date"
    INSUFFICIENT_ROWS = "insufficient_rows"
    SINGLE_CLASS = "single_class"
    EVALUATION_FAILED = "evaluation_failed"


def classify_evaluation_day(
    dataset: pd.DataFrame, *, min_rows: int, label_column: str = "clicked"
) -> EvaluationStatus:
    """평가일 데이터셋을 ROC-AUC 계산 가능 여부로 분류한다(spec §2.3).

    ``evaluate_held_out_roc_auc`` 호출 **전에** 불러 라벨 분포·행 수로 무효 상태를 미리
    가른다 — ``roc_auc_score``의 예외를 사후에 잡아 분류하지 않는다. ``EVALUATION_FAILED``
    (환경·조회 오류)는 이 함수의 판정 대상이 아니다 — 이미 조립에 성공해 데이터프레임을
    받은 경우만 다루며, 조립 자체가 실패하는 경우는 호출부(``run_rolling_origin``)가
    예외로 구분한다.

    우선순위는 행 자체가 없음(``MISSING_DATE``) → 행이 임계치 미만(``INSUFFICIENT_ROWS``)
    → 라벨이 단일 클래스(``SINGLE_CLASS``) 순이다 — 더 근본적인 결손을 먼저 알린다.
    """
    if len(dataset) == 0:
        return EvaluationStatus.MISSING_DATE
    if len(dataset) < min_rows:
        return EvaluationStatus.INSUFFICIENT_ROWS
    if dataset[label_column].nunique() < 2:
        return EvaluationStatus.SINGLE_CLASS
    return EvaluationStatus.VALID


# ============================================================================
# video feature staleness (spec §4) — `days_since_upload`(콘텐츠 나이)와는 다른 값이다.
# staleness = 평가 row의 entity event_timestamp - PIT join이 실제로 고른 video_feature
# 스냅샷의 event_timestamp. `retrieve_training_features`(feast_retrieval.py)가 반환하는
# DataFrame에는 그 소스 타임스탬프가 없으므로(FeatureService가 선언한 피처 컬럼만
# 돌아온다, 코드로 확인), video_feature 테이블에 진단 전용 별도 쿼리를 직접 던진다.
# VideoFeatureView는 ttl=None이라 이 쿼리도 상한 없이 "그 시점 이전 가장 최근 스냅샷"을
# 찾는다 — Feast의 PIT join과 같은 ASOF 규칙이다.
# ============================================================================


def video_feature_snapshot_query(
    *, project: str, dataset: str, video_ids: Sequence[str], as_of: str
) -> tuple[str, bigquery.QueryJobConfig]:
    """PIT join이 고를 video_feature 스냅샷의 event_timestamp를 진단용으로 구하는 SQL.

    학습 조립 경로(``retrieve_training_features``)와는 별개 쿼리이며, 모델 입력에는
    영향을 주지 않는다 — staleness 진단 전용이다. parameterized query로 만든다
    (``autoresearch/loadtest/rerank_fixture.py``의 ``targeted_delete_sql`` 관례를 따름).
    """
    from google.cloud import bigquery

    table_id = f"{project}.{dataset}.video_feature"
    sql = f"""
        SELECT video_id, MAX(event_timestamp) AS selected_ts
        FROM `{table_id}`
        WHERE video_id IN UNNEST(@video_ids)
          AND event_timestamp <= TIMESTAMP(@as_of)
        GROUP BY video_id
    """
    job_config = bigquery.QueryJobConfig(
        query_parameters=[
            bigquery.ArrayQueryParameter("video_ids", "STRING", list(video_ids)),
            bigquery.ScalarQueryParameter("as_of", "STRING", as_of),
        ]
    )
    return sql, job_config


def resolve_video_feature_snapshot_timestamps(
    client: bigquery.Client,
    *,
    project: str,
    dataset: str,
    video_ids: Sequence[str],
    as_of: str,
) -> dict[str, pd.Timestamp]:
    """평가일 데이터셋의 각 영상이 PIT join에서 실제로 골랐을 스냅샷 시각을 구한다.

    반환 dict에 없는 video_id는 그 시점 이전 스냅샷을 못 찾았다는 뜻이다 — 학습 조립
    경로의 cold-start 기본값 대체(``apply_cold_start_defaults``)와 자연히 같은 대상을
    가리키므로, staleness 집계(``compute_video_staleness_summary``)가 별도 조회 없이
    이 "없음"만으로 그 행을 제외할 수 있다.
    """
    if not video_ids:
        return {}
    sql, job_config = video_feature_snapshot_query(
        project=project, dataset=dataset, video_ids=video_ids, as_of=as_of
    )
    frame = client.query(sql, job_config=job_config).to_dataframe()
    return dict(zip(frame["video_id"], frame["selected_ts"]))


class VideoStalenessStatus(str, Enum):
    """video feature staleness 집계 가능 여부."""

    AVAILABLE = "available"
    UNAVAILABLE = "unavailable"


class VideoStalenessSummary(_ResultModel):
    """평가일 하나의 video feature staleness 집계(spec §4)."""

    status: VideoStalenessStatus
    mean_age_days: float | None = None
    max_age_days: float | None = None
    resolved_count: int = 0
    unresolved_count: int = 0
    reason: str | None = None


def compute_video_staleness_summary(
    dataset: pd.DataFrame,
    snapshot_timestamps: Mapping[str, pd.Timestamp],
    *,
    video_id_column: str = "video_id",
    entity_timestamp_column: str = "event_timestamp",
) -> VideoStalenessSummary:
    """resolve된 스냅샷 시각으로 evaluation_entity_timestamp - selected_ts(일)를 집계한다.

    스냅샷을 못 찾은 행(cold-start 대체 대상)은 age 계산에서 제외한다 — 그대로 섞으면
    "오래된 영상"이 "방금 업로드된 영상"처럼 보이는 왜곡이 생긴다(spec §4).
    """
    entity_ts = pd.to_datetime(dataset[entity_timestamp_column])
    ages: list[float] = []
    unresolved = 0
    for video_id, ts in zip(dataset[video_id_column], entity_ts):
        snapshot_ts = snapshot_timestamps.get(video_id)
        if snapshot_ts is None:
            unresolved += 1
            continue
        age_days = (ts - pd.Timestamp(snapshot_ts)).total_seconds() / 86400
        ages.append(age_days)

    if not ages:
        return VideoStalenessSummary(
            status=VideoStalenessStatus.UNAVAILABLE,
            resolved_count=0,
            unresolved_count=unresolved,
            reason=(
                "선택된 video_feature 스냅샷을 하나도 확보하지 못했습니다"
                "(평가 데이터가 비었거나 전부 스냅샷 조회에 실패했습니다)."
            ),
        )
    return VideoStalenessSummary(
        status=VideoStalenessStatus.AVAILABLE,
        mean_age_days=sum(ages) / len(ages),
        max_age_days=max(ages),
        resolved_count=len(ages),
        unresolved_count=unresolved,
    )


# ============================================================================
# 결과 스키마 (spec §2.3) — 평가일 단위 provenance·상태와 degradation_point 판정.
# ============================================================================


class EvaluationSnapshotProvenance(_ResultModel):
    """평가일 하나의 데이터셋 provenance(경량 모델, spec §2.3·Task 3).

    ``TrainingSnapshotManifest``(training_provenance.py)를 재사용하지 않는다 — 그
    모델은 ``registry_generation``/``code_archive_sha`` 등 승격·재현성 감사용 필드까지
    포함해 하루 평가마다 반복 생성하기엔 무겁고, 의미("누가 이 모델을 학습했나")도
    다르다("이 데이터로 무엇을 관측했나").
    """

    dataset_sha256: str
    row_count: int
    positive_count: int
    negative_count: int
    feature_service: str | None
    evaluation_date: str


class PerDayResult(_ResultModel):
    """평가일 하나의 결과(spec §2.3)."""

    date: str
    elapsed_days: int
    status: EvaluationStatus
    roc_auc: float | None = None
    evaluation_provenance: EvaluationSnapshotProvenance | None = None
    video_staleness_summary: VideoStalenessSummary | None = None


class DegradationPoint(_ResultModel):
    """열화 지점 판정 결과(spec §2.4).

    탐지됐으면 ``elapsed_days``/``date``가 채워지고 ``reason``은 ``None``이다.
    탐지되지 않았으면 ``elapsed_days``/``date``가 ``None``이고 ``reason``에 사유
    (``insufficient_valid_points`` 또는 ``no_degradation_detected``)가 남는다.
    """

    elapsed_days: int | None = None
    date: str | None = None
    reason: str | None = None


def compute_min_auc_drop(*, seed_std: float, k: float = 2.0, floor: float = 0.005) -> float:
    """시드 간 변동폭을 열화 판정 임계값으로 변환한다(spec §2.4, plan Task 4).

    ``k=2``는 seed 간 변동폭의 약 두 배보다 작은 하락을 열화로 판정하지 않기 위한
    초기 휴리스틱이다 — 이 저장소의 기존 95% CI 계산 구조(``seed_sweep.py``의
    ``_T_CRITICAL_95``, ``mean ± t_critical × seed_std / sqrt(n)``)를 참고했지만,
    ``k × seed_std`` 자체는 평균의 95% 신뢰구간이 아니다(그러려면 표준오차와 표본
    크기별 t_critical이 필요하다). 개별 관측치 산포를 직접 임계값으로 쓰는 휴리스틱일
    뿐, 통계적으로 보정된 신뢰구간이라고 주장하지 않는다. ``floor``는 ``seed_std``가
    우연히 0에 가까워 임계값이 퇴화하는 것을 막는 하한이다. ``k``·``floor`` 모두
    plan Task 7-A 실측 후 기록되는 초기 설정값이며 최종 정책값이 아니다.
    """
    return max(floor, k * seed_std)


def detect_degradation_point(
    per_day: Sequence[PerDayResult], *, baseline: float, min_auc_drop: float
) -> DegradationPoint:
    """"2개 연속 유효 관측치에서 degraded"가 처음 성립하는 시점을 찾는다(spec §2.4).

    무효일(``status != VALID``)은 유효 관측치 시퀀스에서 아예 제외된다 — 그 사이에
    끼어도 건너뛸 뿐 연속 카운트를 리셋하지 않는다(달력상 연속이 아니라 유효
    관측치 순서상 연속).
    """
    valid_days = [day for day in per_day if day.status == EvaluationStatus.VALID]
    if len(valid_days) < 2:
        return DegradationPoint(reason="insufficient_valid_points")

    threshold = baseline - min_auc_drop
    consecutive = 0
    for day in valid_days:
        if day.roc_auc is not None and day.roc_auc <= threshold:
            consecutive += 1
            if consecutive >= 2:
                return DegradationPoint(elapsed_days=day.elapsed_days, date=day.date)
        else:
            consecutive = 0
    return DegradationPoint(reason="no_degradation_detected")


class RollingOriginResult(_ResultModel):
    """``run_rolling_origin`` 실행 하나의 전체 결과(spec §2.2·§2.3)."""

    cutoff_date: str
    window_days: int
    horizon_days: int
    baseline_val_roc_auc: float
    min_auc_drop: float
    per_day: list[PerDayResult]
    degradation_point: DegradationPoint
    training_snapshot_manifest: TrainingSnapshotManifest


# ============================================================================
# 오케스트레이션 (spec §2.2) — cutoff 학습 1회 + 평가일 순회.
# ============================================================================


class RunRootExistsError(FileExistsError):
    """``run_root``가 이미 존재하고 비어 있지 않은데 ``overwrite``를 지정하지 않았을 때."""


def _prepare_run_root(run_root: Path, *, overwrite: bool) -> tuple[Path, Path]:
    """산출물 경로 격리 계약(spec §2.2)을 위한 디렉터리를 만든다.

    ``run_root``가 이미 존재하고 비어 있지 않으면 ``overwrite=True`` 없이는 막는다 —
    ``require_explicit_experiment_output``(build_training_dataset.py)이 이미 쓰는
    fail-closed 관례와 같다. 이 확인은 어떤 조립·학습 호출보다도 먼저 수행한다.
    """
    if run_root.exists() and any(run_root.iterdir()) and not overwrite:
        raise RunRootExistsError(
            f"{run_root}가 이미 존재하고 비어 있지 않습니다 — 이전 실행 산출물을 "
            "덮어쓰지 않기 위해 멈춥니다. 의도한 재실행이면 overwrite=True를 지정하세요."
        )
    training_dir = run_root / "training"
    evaluation_dir = run_root / "evaluation"
    training_dir.mkdir(parents=True, exist_ok=True)
    evaluation_dir.mkdir(parents=True, exist_ok=True)
    return training_dir, evaluation_dir


def _resolve_staleness_summary(
    dataset: pd.DataFrame,
    date_str: str,
    *,
    bigquery_client: bigquery.Client | None,
    bigquery_project: str | None,
    bigquery_dataset: str | None,
) -> VideoStalenessSummary:
    """staleness 진단에 필요한 조건이 갖춰진 경우에만 실제로 조회한다.

    BigQuery 클라이언트가 없거나(로컬 개발 환경 등) 평가 데이터가 비었으면 조회
    자체를 시도하지 않고 ``UNAVAILABLE``로 남긴다(spec §4 — 부정확한 값을 만들어내지
    않는다).
    """
    if bigquery_client is None or not bigquery_project or not bigquery_dataset or len(dataset) == 0:
        return VideoStalenessSummary(
            status=VideoStalenessStatus.UNAVAILABLE,
            reason=(
                "BigQuery 클라이언트/프로젝트/데이터셋이 제공되지 않았거나 평가 "
                "데이터가 비어 staleness를 계산하지 않았습니다."
            ),
        )
    as_of = f"{date_str} 00:00:00"
    resolved = resolve_video_feature_snapshot_timestamps(
        bigquery_client,
        project=bigquery_project,
        dataset=bigquery_dataset,
        video_ids=dataset["video_id"].unique().tolist(),
        as_of=as_of,
    )
    return compute_video_staleness_summary(dataset, resolved)


def run_rolling_origin(
    cutoff_date: str,
    *,
    window_days: int,
    horizon_days: int,
    run_root: str | Path,
    min_rows_per_day: int,
    min_auc_drop: float,
    min_coverage_days: int | None = None,
    seed: int | None = None,
    feature_service: str | None = None,
    extra_features: Sequence[str] | None = None,
    experiment: str | None = None,
    best_effort: bool = False,
    overwrite: bool = False,
    bigquery_client: bigquery.Client | None = None,
    bigquery_project: str | None = None,
    bigquery_dataset: str | None = None,
) -> RollingOriginResult:
    """단일 cutoff 기반 forward degradation evaluation을 실행한다(spec §2.2).

    cutoff 이전 ``window_days``일로 모델을 1회 학습하고, cutoff부터 ``horizon_days``일을
    하루씩 순회 평가해 ROC-AUC 곡선과 열화 지점을 만든다. 산출물은 ``run_root`` 아래
    ``training/``·``evaluation/<date>/``로 격리되며, 이미 채워진 ``run_root``는
    ``overwrite=True`` 없이는 거부한다(fail-closed, §2.2 "산출물 경로 격리 계약").

    ``min_auc_drop``은 호출부가 미리 정해서 넘긴다(plan Task 7-A calibration) — 이
    함수 자체는 계산하지 않는다. ``best_effort=False``(기본)에서는 평가일 조립 실패
    (``evaluation_failed``)가 전체 실행을 즉시 실패시킨다(§2.3) — 비싼 cutoff 학습을
    버리지 않으려면 ``best_effort=True``로 개별 평가일 실패만 기록하고 계속한다.
    """
    run_root = Path(run_root)
    training_dir, evaluation_dir = _prepare_run_root(run_root, overwrite=overwrite)

    events_start_date, events_end_date = training_window(cutoff_date, window_days)
    training_csv = training_dir / "training_dataset.csv"
    resolved_min_coverage_days = (
        build_training_dataset.DEFAULT_MIN_COVERAGE_DAYS
        if min_coverage_days is None
        else min_coverage_days
    )
    build_training_dataset.main(
        output_path=str(training_csv),
        events_start_date=events_start_date,
        events_end_date=events_end_date,
        min_coverage_days=resolved_min_coverage_days,
        feature_service=feature_service,
        extra_features=extra_features,
    )
    training_manifest = load_training_snapshot_manifest(training_csv)

    model_output = training_dir / "model.joblib"
    feature_columns_output = training_dir / "feature_columns.json"
    categorical_columns_output = training_dir / "categorical_columns.json"
    test_set_output = training_dir / "test_set.csv"
    outcome = train.main(
        data_path=str(training_csv),
        model_output=str(model_output),
        test_set_output=str(test_set_output),
        feature_columns_output=str(feature_columns_output),
        categorical_columns_output=str(categorical_columns_output),
        random_state=seed,
        extra_features=extra_features,
        experiment=experiment,
        # 측정·리포트 산출물이지 승격 후보가 아니다(spec §1 provenance 절과 같은 이유) —
        # 등록 없이 지표만 받는다(window_holdout_eval.py와 같은 관례).
        defer_registration=True,
    )
    baseline = outcome.val_roc_auc

    model = load_model(str(model_output))
    feature_columns = load_feature_columns(str(feature_columns_output))

    per_day: list[PerDayResult] = []
    for date_str, elapsed in evaluation_dates(cutoff_date, horizon_days):
        day_dir = evaluation_dir / date_str
        day_csv = day_dir / "dataset.csv"
        try:
            build_training_dataset.main(
                output_path=str(day_csv),
                events_start_date=date_str,
                events_end_date=date_str,
                # 좁은 단일 날짜 조회는 의도한 것이다 — 학습 spine 커버리지 가드를
                # 우회한다(build_training_dataset.py의 기존 관례, "백필처럼 의도적으로
                # 좁은 구간을 쓸 때는 min_coverage_days=0").
                min_coverage_days=0,
                feature_service=feature_service,
                extra_features=extra_features,
            )
        except Exception:
            if not best_effort:
                raise
            per_day.append(
                PerDayResult(
                    date=date_str,
                    elapsed_days=elapsed,
                    status=EvaluationStatus.EVALUATION_FAILED,
                )
            )
            continue

        dataset = pd.read_csv(day_csv)
        status = classify_evaluation_day(dataset, min_rows=min_rows_per_day)

        roc_auc: float | None = None
        if status == EvaluationStatus.VALID:
            roc_auc = evaluate_held_out_roc_auc(model, dataset, feature_columns)

        positive_count = int(dataset["clicked"].sum()) if len(dataset) else 0
        provenance = EvaluationSnapshotProvenance(
            dataset_sha256=sha256_file(day_csv),
            row_count=len(dataset),
            positive_count=positive_count,
            negative_count=len(dataset) - positive_count,
            feature_service=feature_service,
            evaluation_date=date_str,
        )
        write_manifest_atomic(provenance, day_dir / "dataset_manifest.json")

        staleness_summary = _resolve_staleness_summary(
            dataset,
            date_str,
            bigquery_client=bigquery_client,
            bigquery_project=bigquery_project,
            bigquery_dataset=bigquery_dataset,
        )

        per_day.append(
            PerDayResult(
                date=date_str,
                elapsed_days=elapsed,
                status=status,
                roc_auc=roc_auc,
                evaluation_provenance=provenance,
                video_staleness_summary=staleness_summary,
            )
        )

    degradation_point = detect_degradation_point(
        per_day, baseline=baseline, min_auc_drop=min_auc_drop
    )

    return RollingOriginResult(
        cutoff_date=cutoff_date,
        window_days=window_days,
        horizon_days=horizon_days,
        baseline_val_roc_auc=baseline,
        min_auc_drop=min_auc_drop,
        per_day=per_day,
        degradation_point=degradation_point,
        training_snapshot_manifest=training_manifest,
    )
