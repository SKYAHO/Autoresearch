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
열화 판정의 기준선은 ``per_day`` 중 첫 valid 관측치(``resolve_forward_baseline``)이며,
cutoff 학습의 랜덤 val 지표가 아니다 — 산출 경로가 달라 약 4%p 오프셋이 있어서다
(#485 §4.3이 선행 spec §2.4를 부분 supersede). 전체·최근 구간 지표를 나눠 내고
(``summarize_valid_roc_auc``), 관측 결과에서 재학습 시점을 유도하며
(``derive_hard_retrain_limit`` — 측정과 정책을 분리해 결과 payload에 얹지 않는다),
통계 추정 없이 멈춰야 하는 상태를 판정한다(``evaluate_temporal_hold``).
``run_rolling_origin``이 cutoff 학습 1회 + 평가일 순회를 오케스트레이션하고, 각 실행의
산출물을 ``run_root`` 아래 조건·평가일별로 격리해 이전 실행을 덮어쓰지 않는다(fail-closed).

[비책임] 학습(``train.main``)·데이터 조립(``build_training_dataset.main``)·held-out
ROC-AUC 계산(``evaluate.evaluate_held_out_roc_auc``) 자체는 재구현하지 않고 그대로
호출한다. 승격 판정(``eligible``/``reject``/``hold``)은
``autoresearch/model_evaluation/experiment_evaluation.py`` 소유이며 이 모듈은 호출·수정하지 않는다
(#485 §7.1 게이트 — `#425` 신호 필드를 그 스키마에 얹을지는 `#493` 확인 후 결정).
두 조건(baseline/challenger) 비교와 시간축 paired 계약은 `#514` 소관이다.
Plotly 시각화와 공개 CLI 명령은 각각 ``scripts/bench/``, ``src/cli.py``가 담당한다.
"""

from __future__ import annotations

import math
import shutil
from datetime import datetime, timedelta
from enum import Enum
from pathlib import Path
from typing import TYPE_CHECKING, TypedDict

import pandas as pd
from pydantic import BaseModel, ConfigDict

from autoresearch.model_training import build_training_dataset, train
from autoresearch.model_evaluation.evaluate import evaluate_held_out_roc_auc
from autoresearch.model_training.training_provenance import (
    TrainingSnapshotManifest,
    load_training_snapshot_manifest,
    sha256_file,
    write_manifest_atomic,
)
from autoresearch.model_training.model_utils import load_categorical_columns, load_feature_columns, load_model

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

    ``as_of``는 ``"YYYY-MM-DD HH:MM:SS"`` 형식의 **KST 벽시계 시각**으로 해석한다
    (spine의 날짜 절단 규칙 ``DATE(event_timestamp, 'Asia/Seoul')``,
    ``build_training_dataset.py:201``과 같은 타임존). ``TIMESTAMP(string)``(타임존
    인자 없는 1-인자 형태)은 BigQuery가 UTC로 해석하므로, KST 자정을 의도했는데
    실제로는 KST 09:00 이후 스냅샷까지 포함되는 9시간 오차가 있었다 — 2-인자 형태로
    타임존을 명시해 고정한다.

    ``video_ids``를 하루치 전체를 한 번에 ``ArrayQueryParameter``로 싣는다 —
    표본이 커지면 BigQuery 쿼리 파라미터 크기 상한에 걸릴 수 있다(현재는 진단
    전용·저트래픽이라 배치 분할을 두지 않았다). 트래픽이 커지면 호출부에서
    ``video_ids``를 나눠 여러 번 부르는 배치 분할이 필요하다.
    """
    from google.cloud import bigquery

    table_id = f"{project}.{dataset}.video_feature"
    sql = f"""
        SELECT video_id, MAX(event_timestamp) AS selected_ts
        FROM `{table_id}`
        WHERE video_id IN UNNEST(@video_ids)
          AND event_timestamp <= TIMESTAMP(@as_of, 'Asia/Seoul')
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

    **알려진 근사**: ``snapshot_timestamps``는 하루 전체에 대해 **단일 as_of**(그날
    KST 자정)로 조회한 값이다(``_resolve_staleness_summary``). 실제 PIT join은 각 행의
    ``event_timestamp``를 기준으로 스냅샷을 고르므로, 하루 중 늦게 발생한 행일수록 이
    진단값은 실제보다 **더 오래된(더 stale한) 방향으로 체계적으로 치우친다**(최대
    ~24시간). 정확한 per-row 값을 얻으려면 평가 CSV가 엔티티별 ``event_timestamp``를
    보존해야 하는데, 현재 평가일 조립 계약(``MODEL_FEATURE_COLUMNS + clicked``)은
    이를 보존하지 않는다 — 이 근사는 그 제약 안에서의 의도된 단순화다.
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

    ``run_rolling_origin``도 CLI(``measure-degradation``)도 이 함수를 호출하지
    않는다 — operator가 calibration 단계(plan Task 7-A, 예: ``sweep-seeds``로 구한
    ``seed_std``)에서 이 함수로 값을 미리 계산해 ``--min-auc-drop``으로 직접
    넘기는 것이 계약이다. 자동 배선은 의도적으로 하지 않는다 — calibration은
    cutoff 학습과 별개의(더 비싼) 실행이라, 매 ``measure-degradation`` 호출마다
    반복하면 안 된다.
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
    # 유효일 술어·정렬을 다른 함수와 공유한다(PR #520 리뷰 Low#7·#8) — 같은 결과를
    # 놓고 hold와 판정이 서로 다른 유효일 수를 세면 안 된다.
    valid_days = [day for day in _ordered_by_elapsed(per_day) if is_scorable(day)]
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


def is_scorable(day: PerDayResult) -> bool:
    """이 날이 지표 계산에 쓸 수 있는 관측치인지(#485, PR #520 리뷰 Low#7).

    ``PerDayResult.roc_auc``가 Optional이라 스키마상 ``VALID``인데 점수가 없는 행이
    가능하다. 그 행을 어떤 함수는 세고 어떤 함수는 빼면 hold 판정과 평균이 어긋난다 —
    "유효일"의 정의를 이 한 곳에 모은다. ``#514``가 결과를 재조립하기 시작하면 실제로
    갈릴 수 있는 지점이다.
    """
    return day.status == EvaluationStatus.VALID and day.roc_auc is not None


def count_scorable(per_day: Sequence[PerDayResult]) -> int:
    """유효 평가일 수(PR #527 리뷰 Low#4).

    ``is_scorable``을 한 곳에 모은 것과 같은 이유로 **세는 행위도** 한 곳에 모은다.
    같은 표현이 ``evaluate_temporal_hold``와 ``temporal_signal_inputs``에 각각 있으면,
    한쪽만 바뀌었을 때 "hold와 confidence가 서로 다른 유효일 수를 센다"는 상태가
    조용히 만들어진다.
    """
    return sum(1 for day in per_day if is_scorable(day))


def _ordered_by_elapsed(per_day: Sequence[PerDayResult]) -> list[PerDayResult]:
    """``elapsed_days`` 오름차순으로 정렬한다(PR #520 리뷰 Low#8).

    "첫 관측치"·"최근 N개"는 **리스트 순서가 아니라 시간 순서**여야 한다.
    ``run_rolling_origin`` 산출물은 이미 정렬돼 있지만, JSON 왕복이나 hand-built
    결과를 받는 ``#472``/``#514`` 경로에서는 보장되지 않는다.
    """
    return sorted(per_day, key=lambda day: day.elapsed_days)


def resolve_forward_baseline(
    per_day: Sequence[PerDayResult],
) -> tuple[float | None, int | None]:
    """열화 판정의 기준선을 ``per_day`` 중 **첫 valid 관측치**에서 뽑는다(#485 §4.3).

    ``baseline_val_roc_auc``(cutoff 학습의 랜덤 val 분할 지표)를 기준선으로 쓰면
    산출 경로가 달라 오탐이 난다 — 이 저장소 실측에서 랜덤 val이 forward held-out보다
    약 4%p 높았고(``experiments/2026-07-31_training-window-length/notes.md``),
    그만큼 ``elapsed_days`` 0~1에서 "열화"로 잘못 잡힌다. 같은 산출 경로(forward
    held-out)끼리 비교해 그 오프셋을 **정의상 상쇄**한다.

    새 계산 경로가 없다 — ``run_rolling_origin``이 이미 만든 ``per_day`` 값을 읽기만
    한다. ``PerDayResult``가 ``allow_inf_nan=False``라 여기서 나오는 값은 유한값이거나
    ``None``임이 스키마로 보장된다(별도 ``math.isfinite`` 검증이 없는 이유).

    Returns:
        (첫 valid 관측치의 roc_auc, 그 관측치의 elapsed_days). valid가 하나도 없으면
        ``(None, None)``.
    """
    for day in _ordered_by_elapsed(per_day):
        if is_scorable(day):
            return day.roc_auc, day.elapsed_days
    return None, None


def summarize_valid_roc_auc(
    per_day: Sequence[PerDayResult], *, recent_window_days: int
) -> tuple[float | None, float | None]:
    """전체 구간과 최근 구간의 ROC-AUC 평균을 나눠 낸다(#485 §3).

    **날 동등 가중** 평균이다 — 행 수가 많은 날이 지표를 지배하면 "날의 평균"이 아니라
    "행의 평균"이 된다. 열화는 날 단위 현상이므로 날마다 같은 무게를 준다(#506이
    grouped AUC에서 매크로 평균을 택한 것과 같은 근거).

    "최근"은 **최근 N개 유효일**이지 최근 N일이 아니다 — 무효일이 사이에 끼면 달력상
    더 멀리까지 거슬러 올라간다. 결손일을 빈 값으로 세면 표본이 조용히 줄어든다.

    Returns:
        (overall, recent). 유효일이 없으면 overall은 ``None``, 유효일이
        ``recent_window_days`` 미만이면 recent는 ``None``이다 — 적은 표본으로 평균을
        만들어 "최근 성능"이라고 부르지 않는다.
    """
    if recent_window_days < 1:
        # 0이면 valid_scores[-0:]가 전체 리스트가 되어 recent와 overall이 같은 값이
        # 되고, 음수면 앞쪽을 잘라낸 나머지의 평균이 "최근"으로 나간다 — 둘 다 None도
        # 예외도 아니라 소비자가 잘못을 알 수 없다(PR #520 리뷰 Medium#4).
        raise ValueError(
            f"recent_window_days는 1 이상이어야 합니다(받은 값: {recent_window_days})."
        )

    valid_scores = [day.roc_auc for day in _ordered_by_elapsed(per_day) if is_scorable(day)]
    if not valid_scores:
        return None, None

    overall = sum(valid_scores) / len(valid_scores)
    if len(valid_scores) < recent_window_days:
        return overall, None
    recent_scores = valid_scores[-recent_window_days:]
    return overall, sum(recent_scores) / len(recent_scores)


class RollingOriginResult(_ResultModel):
    """``run_rolling_origin`` 실행 하나의 전체 결과(spec §2.2·§2.3)."""

    cutoff_date: str
    window_days: int
    horizon_days: int
    # cutoff 학습의 랜덤 val 지표. **판정에는 쓰지 않는다**(#485 §4.3이 선행 spec §2.4를
    # 부분 supersede) — 참고용·과거 실행과의 비교용으로 유지한다(하위호환).
    baseline_val_roc_auc: float
    # 열화 판정의 실제 기준선. per_day 중 첫 valid 관측치의 roc_auc(#485 §4.3).
    forward_baseline_roc_auc: float | None = None
    # 그 기준선을 준 관측치의 elapsed_days. 기준선이 어느 날에서 왔는지 사후에
    # 추적할 수 있어야 한다 — 결손이 앞에 끼면 0이 아닐 수 있다.
    forward_baseline_source: int | None = None
    min_auc_drop: float
    # 전체 평가 기간의 성능(유효일 날 동등 가중 평균). 유효일 0개면 None.
    overall_roc_auc_mean: float | None = None
    # 최근 recent_window_days개 **유효일**의 평균. 유효일이 그보다 적으면 None —
    # 적은 표본으로 평균을 만들어 "최근 성능"이라고 부르지 않는다(#485 §3).
    recent_roc_auc_mean: float | None = None
    # 위 "최근"의 폭. 실측 후 재조정 대상이라 결과에 함께 남겨 사후 해석이 가능하게 한다.
    recent_window_days: int = 3
    per_day: list[PerDayResult]
    degradation_point: DegradationPoint
    training_snapshot_manifest: TrainingSnapshotManifest


class TemporalSignalInputs(TypedDict):
    """``summarize_temporal_signal``의 키워드 인자 계약(PR #527 리뷰 Low#5).

    ``dict[str, object]``로 두면 ``**`` 언패킹 시 모든 인자가 ``object``가 되어 키 오타도
    타입 불일치도 정적으로 드러나지 않는다. ``TypedDict``는 ``experiment_evaluation``을
    import하지 않으므로, 이 모듈의 ML 의존이 판정 경로로 새지 않는다는 결정(아래
    docstring)과 충돌하지 않는다.
    """

    degradation_elapsed_days: int | None
    recent_roc_auc_mean: float | None
    valid_day_count: int
    recent_window_days: int


def temporal_signal_inputs(result: RollingOriginResult) -> TemporalSignalInputs:
    """판정 엔진의 ``summarize_temporal_signal``에 넘길 원시값을 뽑는다(#485 §5.3).

    이 모듈은 ``experiment_evaluation``을 **import하지 않는다** — 반대 방향(판정 엔진이
    이 모듈을 import)도 마찬가지다. 판정 엔진은 지금 ML 의존이 전혀 없는데, 이 모듈은
    ``train``(→ lightgbm)을 끌고 오기 때문이다. 그래서 값만 뽑아 주고 신호 계산은
    판정 엔진이 한다:

    ```python
    signal = summarize_temporal_signal(**temporal_signal_inputs(result))
    ```

    ``offline_primary_delta``/``temporal_delta``는 여기서 채우지 않는다 — 두 조건 비교가
    있어야 나오는 값이라 ``#514`` 이후에 호출부가 따로 넘긴다.

    **주의(PR #527 리뷰 Low#5)**: ``valid_day_count``는 ``per_day``에서 **다시 세지만**
    같은 결과의 ``recent_roc_auc_mean``은 측정 시점에 이미 확정된 값이다. 지금은 한
    번의 ``run_rolling_origin``이 둘을 같은 ``per_day``로 만들어 어긋날 수 없다. 다만
    ``#514``가 결과를 재조립하기 시작하면(``is_scorable`` docstring이 지목한 지점)
    ``per_day``만 갈아끼운 결과에서 두 값이 서로 다른 관측을 근거로 삼을 수 있다.
    """
    return {
        "degradation_elapsed_days": result.degradation_point.elapsed_days,
        "recent_roc_auc_mean": result.recent_roc_auc_mean,
        "valid_day_count": count_scorable(result.per_day),
        "recent_window_days": result.recent_window_days,
    }


class HardRetrainLimit(_ResultModel):
    """성능과 무관하게 재학습해야 하는 시점까지의 일수(#485 §4.1).

    ``limit_days``가 ``None``이면 **값을 만들 수 없었다**는 뜻이고 ``reason``에 왜인지
    남는다. "안전하다"는 뜻이 아니다 — 그 둘을 같은 값으로 표현하지 않는 것이 이
    계약의 핵심이다.
    """

    limit_days: int | None = None
    reason: str | None = None


def derive_hard_retrain_limit(
    result: RollingOriginResult, *, safety_margin_days: int
) -> HardRetrainLimit:
    """측정 결과에서 hard retrain limit을 유도한다(#485 §4.1·§4.2).

    ``RollingOriginResult``에 얹지 않고 **별도 함수**로 둔다 — 측정(관측 사실)과
    정책(운영 판단)을 같은 payload에 섞지 않기 위해서다. 값을 ``#461`` 승격 게이트에
    배선하는 것은 ``#472`` 소유이며, ``next_retrain_at``도 여기서 계산하지 않는다
    (``last_trained_at``은 이 모듈이 모르는 값이다).

    **소비 순서 — 호출부가 ``evaluate_temporal_hold``를 먼저 확인해야 한다.**
    이 함수는 hold를 참조하지 않으므로, ``temporal_ordering_violated``(곡선 자체가
    오염됨)나 ``temporal_horizon_incomplete``(잘린 구간)인 결과에서도 숫자 모양이
    정상인 ``limit_days``를 돌려준다. hold 사유가 있으면 이 값을 **무시해야 한다**
    (spec §4.2 "소비 순서"). 함수 안에서 강제하지 않는 이유는 hold가 ``#461`` 게이트의
    별도 신호이고, 여기 숨기면 소비자가 hold 자체를 따로 볼 수 없기 때문이다.

    **관측되지 않은 것을 "안전"으로 바꾸지 않는다.** ``degradation_point``가 없으면
    값을 만들지 않고 사유만 남긴다 — ``horizon_days``가 짧아서 못 본 것과 실제로 안
    꺾인 것은 다른 사실인데, 하한값으로 채우면 둘이 같은 숫자가 된다.

    두 미탐지 사유는 **구분해서** 전달한다:

    - ``insufficient_valid_points``: 그대로 전달한다. 측정 단계의 사실이 정책 단계에서
      다른 말로 바뀌면 원인 추적이 끊긴다.
    - ``no_degradation_detected`` → ``no_degradation_observed_within_horizon``:
      이름을 바꾸는 유일한 경우다. 원래 사유는 곡선에 대한 진술("열화가 탐지되지
      않았다")인데, 정책 관점에서 중요한 건 **관측 범위 안에서만** 그렇다는 한정이다.
      뭉개는 게 아니라 한계를 드러내는 방향으로 좁힌다.
    """
    if safety_margin_days < 0:
        # 음수면 limit_days가 degradation_point.elapsed_days보다 커지고 reason도 None으로
        # 남아, 소비자가 잘못된 입력이었음을 알 방법이 없다(PR #520 리뷰).
        raise ValueError(
            f"safety_margin_days는 0 이상이어야 합니다(받은 값: {safety_margin_days})."
        )

    degradation_point = result.degradation_point
    if degradation_point.elapsed_days is None:
        reason = degradation_point.reason
        if reason == "no_degradation_detected":
            reason = "no_degradation_observed_within_horizon"
        return HardRetrainLimit(limit_days=None, reason=reason)

    limit_days = degradation_point.elapsed_days - safety_margin_days
    if limit_days < 0:
        # 여유 기간이 열화 시점보다 크면 "이미 지났다"는 뜻이다. 음수 일수는 의미가
        # 없으므로 0으로 clamp하되, 그 사실을 사유로 남겨 조용히 뭉개지 않는다.
        return HardRetrainLimit(
            limit_days=0, reason="safety_margin_exceeds_degradation_point"
        )
    return HardRetrainLimit(limit_days=limit_days)


# ============================================================================
# fail-closed `hold` 종료 조건 (#485 §6) — 통계 추정 없이 멈춰야 하는 상태.
# `condition_mismatch`(두 조건의 cutoff·window·horizon·snapshot·split·seed 불일치)는
# 두 조건 비교가 전제라 `#514` 소관이며 여기서 다루지 않는다.
# ============================================================================


class TemporalHoldReason(str, Enum):
    """temporal 평가를 `hold`로 끝내야 하는 사유(#485 §6)."""

    TEMPORAL_EVIDENCE_MISSING = "temporal_evidence_missing"
    TEMPORAL_ORDERING_VIOLATED = "temporal_ordering_violated"
    TEMPORAL_HORIZON_INCOMPLETE = "temporal_horizon_incomplete"
    TEMPORAL_INSUFFICIENT_VALID_POINTS = "temporal_insufficient_valid_points"


def evaluate_temporal_hold(
    result: RollingOriginResult | None,
) -> TemporalHoldReason | None:
    """통계 추정 없이 멈춰야 하는 상태인지 판정한다(#485 §6).

    Returns:
        ``hold`` 사유. ``None``이면 hold가 아니다(정상 진행).

    판정 순서는 **evidence 부재 → 시간 순서 위반 → 미래 구간 누락 → 데이터 부족**이다.
    더 근본적인 결손을 먼저 보고한다 — 선행 spec §2.3의 ``classify_evaluation_day``가
    "행 없음 → 행 부족 → 단일 클래스" 순으로 가르는 것과 같은 원칙이다. 예를 들어
    구간이 잘려서(``horizon_incomplete``) 표본이 모자란(``insufficient_valid_points``)
    경우, 원인인 앞쪽을 보고해야 운영자가 고칠 곳을 안다.
    """
    if result is None:
        return TemporalHoldReason.TEMPORAL_EVIDENCE_MISSING

    # 학습 구간이 cutoff 당일 이후까지 뻗으면 학습이 평가 구간을 본 것이다.
    # 선행 spec §2.1이 events_end_date = cutoff - 1일로 고정하므로 정상 경로에서는
    # 나올 수 없다 — 호출부가 결과를 손으로 만들어 넣는 경우를 막는 심층 방어다
    # (#478의 "producer가 보낸 숫자를 믿지 않는다"와 같은 결).
    cutoff = datetime.strptime(result.cutoff_date, _DATE_FORMAT).date()
    if result.training_snapshot_manifest.events_end_date >= cutoff:
        return TemporalHoldReason.TEMPORAL_ORDERING_VIOLATED

    # (a) 길이 자체가 모자란 경우. `run_rolling_origin`은 모든 평가일에 대해
    #     PerDayResult를 반드시 하나씩 append하므로 정상 산출물에서는 성립하지 않는다 —
    #     hand-built 결과에 대한 심층 방어다.
    if len(result.per_day) < result.horizon_days:
        return TemporalHoldReason.TEMPORAL_HORIZON_INCOMPLETE

    # (b) 길이는 맞는데 **꼬리가 통째로 결손**인 경우. 이게 실제 운영 케이스다
    #     (PR #520 리뷰 Medium#3): `cutoff+H`가 아직 지나지 않았거나 데이터 레이크가
    #     뒤처진 시점에 실행하면 뒷날들이 missing_date로 채워져 (a)를 통과한다. 그대로
    #     두면 잘린 구간 위에서 계산된 평균·열화 시점이 그대로 나간다.
    #     중간 결손은 여기 해당하지 않는다 — 관측이 horizon 끝까지는 도달했기 때문이다.
    ordered = _ordered_by_elapsed(result.per_day)
    if ordered and ordered[-1].status == EvaluationStatus.MISSING_DATE:
        return TemporalHoldReason.TEMPORAL_HORIZON_INCOMPLETE

    # 점수가 없는 VALID 행은 유효일로 세지 않는다 — 평균·기준선과 같은 술어를 쓴다
    # (PR #520 리뷰 Low#7).
    valid_days = count_scorable(result.per_day)
    if valid_days < 2:
        return TemporalHoldReason.TEMPORAL_INSUFFICIENT_VALID_POINTS

    return None


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

    ``overwrite=True``면 기존 ``run_root``를 **완전히 비운 뒤** 다시 만든다 — 그냥
    ``mkdir(exist_ok=True)``만 하면 이전 실행의 ``evaluation/<date>/`` 산출물이 이번
    실행에 속하지 않는 날짜까지 남아, 결과 JSON과 디스크 내용이 어긋난다(예:
    ``horizon_days=30``으로 돌린 뒤 ``horizon_days=7``로 재실행하면 8~30일차 잔재가
    "성공한 날"처럼 남는다).
    """
    if run_root.exists() and any(run_root.iterdir()):
        if not overwrite:
            raise RunRootExistsError(
                f"{run_root}가 이미 존재하고 비어 있지 않습니다 — 이전 실행 산출물을 "
                "덮어쓰지 않기 위해 멈춥니다. 의도한 재실행이면 overwrite=True를 "
                "지정하세요."
            )
        shutil.rmtree(run_root)
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

    평가일 CSV는 ``MODEL_FEATURE_COLUMNS + extra_features + "clicked"``만 담고
    (``build_training_dataset.py:690``), ``video_id``/``event_timestamp``(엔티티
    키)는 없다 — 이 함수를 부르려면 그 두 컬럼이 필요하므로, 없으면 조회를 시도하지
    않고 사유를 남긴다. 컬럼을 보존하도록 조립 계약을 확장하는 것은 이 PR 범위 밖이다
    (측정 하네스 자체의 스키마 변경이라 별도 합의가 필요하다).
    """
    if bigquery_client is None or not bigquery_project or not bigquery_dataset or len(dataset) == 0:
        return VideoStalenessSummary(
            status=VideoStalenessStatus.UNAVAILABLE,
            reason=(
                "BigQuery 클라이언트/프로젝트/데이터셋이 제공되지 않았거나 평가 "
                "데이터가 비어 staleness를 계산하지 않았습니다."
            ),
        )
    missing_columns = [
        column for column in ("video_id", "event_timestamp") if column not in dataset.columns
    ]
    if missing_columns:
        return VideoStalenessSummary(
            status=VideoStalenessStatus.UNAVAILABLE,
            reason=(
                f"평가 데이터셋에 {missing_columns}가 없어 staleness를 계산할 수 "
                "없습니다(현재 평가 CSV 스키마는 MODEL_FEATURE_COLUMNS+clicked만 "
                "포함하며 엔티티 키를 보존하지 않습니다)."
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
    recent_window_days: int = 3,
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

    ``min_auc_drop``은 호출부가 미리 정해서 넘긴다(plan Task 7-A calibration,
    ``compute_min_auc_drop`` 참고) — 이 함수는 그 계산을 호출하지 않는다; operator가
    ``sweep-seeds`` 등으로 미리 calibration한 값을 ``--min-auc-drop``으로 직접
    넘긴다. ``best_effort=False``(기본)에서는 평가일 하나의 실패(조립·상태 판정·
    ROC-AUC 계산·provenance 기록 전체)가 전체 실행을 즉시 실패시킨다(§2.3) — 비싼
    cutoff 학습을 버리지 않으려면 ``best_effort=True``로 개별 평가일 실패만
    ``evaluation_failed``로 기록하고 계속한다.

    **판정 기준선(#485 §4.3, 선행 spec §2.4를 부분 supersede)**: 열화 판정에는
    ``forward_baseline_roc_auc``(``per_day`` 중 첫 valid 관측치)를 쓴다.
    ``baseline_val_roc_auc``(cutoff 학습의 랜덤 val 분할 지표)는 결과 필드로만 남기고
    **판정에는 쓰지 않는다** — 두 값은 산출 경로도 대상 분포도 달라서, 이 저장소 실측
    (``experiments/2026-07-31_training-window-length/notes.md`` "홀드아웃 값이 val보다
    4%p 낮다")에서 랜덤 val이 실제 다음 날 성능보다 **약 4%p 높게** 나왔다. 그 상태로
    비교하면 ``elapsed_days`` 0~1에서 오탐이 난다. 같은 산출 경로(forward held-out)
    끼리 비교해 그 오프셋을 **정의상 상쇄**한다.

    남은 트레이드오프: 기준선이 다수 행의 val 분할에서 **평가일 관측치 1개**로 바뀌었으므로,
    그날의 표본 변동이 기준선에 그대로 실린다. 계통 오프셋을 없앤 대신 날 단위 분산이
    threshold에 들어온 셈이다 — ``forward_baseline_source``를 결과에 남겨 어느 날이
    기준선이 됐는지 사후에 추적할 수 있게 했다(spec §4.3).
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
    if not math.isfinite(baseline):
        # TrainingOutcome.val_roc_auc의 기본값이 NaN이다(#445와 같은 이유 —
        # 학습을 거치지 않은 outcome이 섞이면 여기로 들어온다). 여기서 멈추지 않으면
        # horizon_days일치 평가를 전부 마친 뒤 RollingOriginResult 생성 시점에서야
        # allow_inf_nan=False로 실패해, 이미 끝난 평가 결과가 통째로 버려진다.
        raise ValueError(
            f"cutoff 학습의 val_roc_auc가 유한하지 않습니다({baseline!r}) — 학습이 "
            "실제로 수행됐는지 확인하세요. horizon_days 평가를 시작하기 전에 멈춥니다."
        )

    model = load_model(str(model_output))
    feature_columns = load_feature_columns(str(feature_columns_output))
    categorical_categories = load_categorical_columns(str(categorical_columns_output))

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

            dataset = pd.read_csv(day_csv)
            status = classify_evaluation_day(dataset, min_rows=min_rows_per_day)

            roc_auc: float | None = None
            if status == EvaluationStatus.VALID:
                # 학습 시점 category→code 매핑을 그대로 재현해야 LightGBM이 같은
                # 스플릿을 적용한다(src/serving/service.py:90-99와 같은 패턴). 이걸
                # 안 하면 evaluate_held_out_roc_auc 내부의 무조건 astype("category")가
                # 그날 데이터에 실제 등장한 값만으로 카테고리를 다시 매겨, 카테고리
                # 구성이 날마다 달라질 때(예: category_id) 모델이 학습 때와 다른
                # 코드로 예측한다 — 그 오차가 elapsed_days가 커질수록 누적되면
                # "진짜 열화"와 구분 안 되는 가짜 하락 곡선이 나온다.
                for column, categories in categorical_categories.items():
                    if column in dataset.columns:
                        dataset[column] = pd.Categorical(dataset[column], categories=categories)
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
        except Exception:
            # spec §2.3의 evaluation_failed는 조립뿐 아니라 "환경·조회 오류(스키마
            # 불일치·모델 로드 실패 등)"까지 포괄한다 — 그래서 이 평가일의 나머지
            # 단계(읽기·상태 판정·ROC-AUC·provenance 기록·staleness 조회)도 전부
            # try 안에 둔다. best_effort=False면 여기서 즉시 중단해 원인을 드러낸다.
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

    # 판정 기준선은 랜덤 val이 아니라 forward held-out이다(#485 §4.3, 선행 spec §2.4를
    # 부분 supersede). baseline(=val_roc_auc)은 아래 결과 필드로만 남는다.
    forward_baseline, forward_baseline_source = resolve_forward_baseline(per_day)
    if forward_baseline is None:
        # valid 관측치가 하나도 없으면 비교할 기준선 자체가 없다. detect_degradation_point도
        # 같은 입력에서 insufficient_valid_points를 내지만, 여기서 먼저 끝내 "기준선 없이
        # 판정했다"는 상태를 만들지 않는다.
        degradation_point = DegradationPoint(reason="insufficient_valid_points")
    else:
        degradation_point = detect_degradation_point(
            per_day, baseline=forward_baseline, min_auc_drop=min_auc_drop
        )

    overall_mean, recent_mean = summarize_valid_roc_auc(
        per_day, recent_window_days=recent_window_days
    )

    return RollingOriginResult(
        cutoff_date=cutoff_date,
        window_days=window_days,
        horizon_days=horizon_days,
        baseline_val_roc_auc=baseline,
        forward_baseline_roc_auc=forward_baseline,
        forward_baseline_source=forward_baseline_source,
        min_auc_drop=min_auc_drop,
        overall_roc_auc_mean=overall_mean,
        recent_roc_auc_mean=recent_mean,
        recent_window_days=recent_window_days,
        per_day=per_day,
        degradation_point=degradation_point,
        training_snapshot_manifest=training_manifest,
    )
