#!/usr/bin/env python3
"""training_dataset.csv 생성 파이프라인 — offline feature store PIT 조회 (#359 C2로 feast-only).

[파이프라인] 피처 구간 — spine(``training_entity``, KST 날짜 폐구간 [start, end])에 21개 모델
피처를 Feast ``get_historical_features``(point-in-time)로 붙여 CSV로 쓴다(``_assemble_via_feast``).
#359 C2에서 DuckDB 재계산 경로(raw에서 자체 계산)를 제거하고 feast를 **유일 경로**로 만들었다 —
offline store가 정본(#357)이라 그 값을 그대로 읽는다.

[기능] ``main()``은 조립 전에 ``_verify_assembly_environment()``로 실행 가능 여부를 fail-fast
확인한다(환경변수 → GCP 자격증명 → feast import 순, #404) — 자격증명 없는 환경에서 BigQuery
접속이 응답 없이 멈추는 대신 즉시 명확한 이유로 중단한다.
BigQuery 프로젝트는 ``CTR_TRAINING_BQ_PROJECT``로 명시해야 하며, 이 설정은 모든
BigQuery/Feast import와 클라이언트 생성보다 먼저 앞뒤 공백을 정규화해 검증한다.

spine 로드 직후에는 ``summarize_spine_coverage``/``require_spine_coverage``로 **요청 기간 대비
실제 확보한 날짜 수**를 검증한다(#464). 기간 조회는 없는 파티션을 에러가 아니라 "행 없음"으로
돌려주므로, 이 검사가 없으면 데이터가 며칠씩 비어도 조립·학습이 조용히 성공한다 — champion
v12가 사실상 2일치로 학습돼 재현 불가능한 지표로 굳었던 사고의 직접 원인이다. 두 함수는
BigQuery 없이 단위 테스트 가능한 순수 함수이며, 검증은 비싼 PIT 조회 **전에** 수행한다.
백필처럼 의도적으로 좁은 구간을 쓸 때는 ``min_coverage_days=0``으로 우회한다. 실측 커버리지는
``SpineCoverage``로 호출부에 돌려주며, ``run-pipeline``이 MLflow lineage에 남겨 "요청 구간 ≠
실제 학습 구간"을 사후에 판별할 수 있게 한다. 기준값 근거·동작 계약·이 가드가 막지 못하는
것은 ``docs/specs/2026-08-01-training-window-coverage-guard.md``가 정본이다.

출력: data/processed/training_dataset.csv와 snapshot sidecar. 컬럼은
``[*MODEL_FEATURE_COLUMNS, *extra_features, "clicked"]`` 순서이며, ``extra_features``가 비면
기존 계약(21 모델 피처 + ``clicked`` label = 22 물리 컬럼)과 동일하다. 실험이
``feature_service``/``extra_features``를 주면 그 FeatureService로 조회하고 선언한 파생 피처를
잘라내지 않고 보존한다(#454) — 학습의 ``--extra-features``는 데이터셋에 **이미 있는** 컬럼만
승격하므로, 조립이 보존하지 않으면 가설이 성립하지 않는다. 계약 정본은
``docs/specs/2026-08-03-paired-offline-experiment-comparison.md`` §2다.
model input의 이름·순서·categorical 분류는 ``src/features/model_contract.py``가, staged PIT 조회는
``src/features/feast_retrieval.py``가 소유한다(이 모듈은 재정의하지 않는다).

[비책임] 학습 조립(feast)이 쓰지 않지만 인접 소비자와 공유하느라 이 모듈에 남은 헬퍼:
``derive_wide_events``(long→wide attribution)·``load_events_from_bigquery``는 일일추천
(``daily_recommendations``)이, ``load_personas``(+ ``to_personas_frame`` import)는 정책 시뮬레이션
(``simulate_policy_round``)과 벤치(``scripts/bench/bench_feature_assembly.py``)가 쓴다 —
이들의 feast 전환(#359 C3)에서 함께 정리한다. DuckDB 재계산·mock CSV 입력 경로는 #359 C2에서 제거됐다.
"""

from __future__ import annotations

import os
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path
from tempfile import NamedTemporaryFile, TemporaryDirectory
from urllib.parse import urlparse

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.features.assembly import connect_duckdb  # noqa: E402
from src.features.model_contract import (  # noqa: E402
    MODEL_FEATURE_COLUMNS,
    PASSTHROUGH_COLUMNS,
    FeatureContractError,
    resolve_experiment_feature_columns,
)
from src.pipeline.virtual_user_adapter import to_personas_frame  # noqa: E402
from src.pipeline.training_provenance import (  # noqa: E402
    ProvenanceValidationError,
    RegistryProvenance,
    build_snapshot_manifest,
    sha256_file,
    snapshot_manifest_path,
    write_manifest_atomic,
)

# spine 커버리지 가드(#464). 요청 기간에 데이터 없는 날이 섞여도 조용히 성공하던 것을 막는다 —
# champion v12가 사실상 2일치로 학습돼 재현 불가능한 val_roc_auc=0.80으로 굳고, 이후 정상
# 모델의 승격을 전부 막았던 사고가 근거다(2026-07-31 조사).
#
# 기준값의 근거·동작 계약·한계는 docs/specs/2026-08-01-training-window-coverage-guard.md가
# 정본이다(실측으로 확정). 요약:
# - MIN_ROWS_PER_DAY: 붕괴일 240행 vs 정상일 125,760~167,592행 — 두 군집이 500배 떨어져 있어
#   그 사이 어디에 두든 판정이 같다. 5,000은 경계에 민감하지 않은 값으로 골랐다.
# - MIN_COVERAGE_DAYS: **정확도 최적점이 아니라 사고 재발 차단선이다.** 공통 홀드아웃(07-29)
#   에서 정상 2일과 13일의 차이가 없었다(Δ=+0.0039, 노이즈 경계 0.0184) — 이 가드를 "날짜가
#   많을수록 좋은 모델"의 근거로 쓰면 안 된다. 가드의 목적은 요청한 기간과 실제 학습된
#   기간의 불일치를 드러내는 것이고, 3일은 v12(정상 2일)를 막고 v18(정상 4일)은 통과시키는 선이다.
DEFAULT_MIN_COVERAGE_DAYS = int(os.environ.get("CTR_TRAINING_MIN_COVERAGE_DAYS", "3"))
DEFAULT_MIN_ROWS_PER_DAY = int(os.environ.get("CTR_TRAINING_MIN_ROWS_PER_DAY", "5000"))

BIGQUERY_PROJECT = os.environ.get("CTR_TRAINING_BQ_PROJECT")
# feature/서빙 계층 dataset — Feast feature 테이블 4종과 배치 출력 테이블(user_recommendations).
BIGQUERY_DATASET = os.environ.get("CTR_TRAINING_BQ_DATASET", "feast_offline_store")
# raw(데이터 레이크) 계층 dataset — data_lake_* 테이블 전용(feature 계층과 물리 분리).
BIGQUERY_RAW_DATASET = os.environ.get("CTR_TRAINING_BQ_RAW_DATASET", "data_lake_raw")
BIGQUERY_ACTION_LOG_TABLE = os.environ.get(
    "CTR_TRAINING_BQ_ACTION_LOG_TABLE", "data_lake_action_log"
)
# derive_wide_events attribution 윈도우(daily_recommendations가 공유하는 헬퍼). impression→click
# 귀속(label) / click→view→like 체이닝(followup). docs/guides/data-warehouse.md training_entity.
LABEL_WINDOW_SEC = int(os.environ.get("CTR_TRAINING_LABEL_WINDOW_SEC", "1800"))
FOLLOWUP_WINDOW_SEC = int(os.environ.get("CTR_TRAINING_FOLLOWUP_WINDOW_SEC", "600"))


def require_bigquery_project() -> str:
    """명시적으로 설정된 정규화 BigQuery 프로젝트를 반환한다."""
    project = (BIGQUERY_PROJECT or "").strip()
    if not project:
        raise ValueError("CTR_TRAINING_BQ_PROJECT 환경변수가 필요합니다")
    return project


def raw_table_id(table: str) -> str:
    """raw(데이터 레이크) 테이블의 완전한 BigQuery 식별자를 만든다.

    raw 테이블(`data_lake_*`)은 feature 계층과 다른 dataset
    (`CTR_TRAINING_BQ_RAW_DATASET`, 기본 `data_lake_raw`)에 있다. 모듈
    전역을 호출 시점에 읽으므로 테스트에서 monkeypatch 로 재정의할 수 있다.
    """
    return f"{require_bigquery_project()}.{BIGQUERY_RAW_DATASET}.{table}"


def feature_table_id(table: str) -> str:
    """feature/서빙 테이블의 완전한 BigQuery 식별자를 만든다.

    Feast feature 테이블과 배치 출력 테이블은 계속
    `CTR_TRAINING_BQ_DATASET`(기본 `feast_offline_store`)에 있다.
    """
    return f"{require_bigquery_project()}.{BIGQUERY_DATASET}.{table}"


def get_data_dir():
    """프로젝트 루트의 data 디렉토리 경로 반환. 없으면 프로젝트 루트 아래에 생성한다.

    GCS 코드 부트스트랩 이미지(Dockerfile.train)는 data/를 이미지에 포함하지
    않으므로, 컨테이너 최초 실행 시에는 이 디렉토리가 아예 존재하지 않는다 —
    존재를 요구하는 대신 만들어서 돌려준다(출력 경로 등으로 바로 쓰기 위함).
    """
    current = os.path.dirname(os.path.abspath(__file__))
    while current != "/":
        if os.path.exists(os.path.join(current, "data")):
            return os.path.join(current, "data")
        current = os.path.dirname(current)
    data_dir = os.path.join(PROJECT_ROOT, "data")
    os.makedirs(data_dir, exist_ok=True)
    return data_dir


def load_personas(personas_path: str) -> pd.DataFrame:
    """personas 입력을 확장자로 판별해 로드한다.

    로컬/GCS 경로 모두 지원한다(gcsfs가 gs:// 경로를 pandas에 투명하게
    연결한다). CSV는 이미 personas 계약(uuid/age/occupation/관심사) 형태인
    mock 산출물이라 그대로 쓴다. parquet은 virtual_users 파이프라인의 원본
    스키마(user_id/hobby_keywords/interest_keywords 등)이므로
    to_personas_frame()으로 계약 형태로 정규화한다(daily_recommendations.py와
    동일한 패턴, #229).
    """
    if personas_path.endswith(".parquet"):
        return to_personas_frame(pd.read_parquet(personas_path))
    return pd.read_csv(personas_path)


def load_events_from_bigquery(start_date: str, end_date: str) -> pd.DataFrame:
    """dt 파티션 [start_date, end_date] 범위의 raw long-format 이벤트를
    그대로 가져온다. attribution(long→wide 변환)은 여기서 하지 않는다 —
    derive_wide_events()가 DuckDB로 순수하게 수행한다. BigQuery SQL 안에서
    조인하면 attribution 로직을 실제 데이터로 단위 테스트할 방법이 없어서
    조회와 변환을 분리한다. (이 함수와 derive_wide_events는 #359 C2 이후
    일일추천(daily_recommendations)만 쓰는 공유 헬퍼다 — 학습 조립은 feast 경로.)

    start_date/end_date는 dt 파티션 필터용 KST 캘린더 날짜 문자열
    (YYYY-MM-DD)이다. dt 자체가 timezone 없이 생성 시점에 이미 Asia/Seoul
    날짜 경계로 버킷팅되어 있으므로 여기서 timezone 변환은 하지 않는다.
    """
    project = require_bigquery_project()

    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    query = f"""
        SELECT event_id, event_timestamp, user_id, event_type, video_id, watch_time_sec
        FROM `{raw_table_id(BIGQUERY_ACTION_LOG_TABLE)}`
        WHERE dt BETWEEN '{start_date}' AND '{end_date}'
    """
    return client.query(query).to_dataframe()


def load_training_entity_spine(start_date: str, end_date: str) -> pd.DataFrame:
    """training_entity(spine)를 KST 날짜 폐구간 [start, end]로 로드한다(#358 feast 경로).

    spine은 feature_store_build가 적재한 (user_id, video_id, event_timestamp, clicked)
    이며, feast get_historical_features의 entity dataframe이 된다. DuckDB 경로처럼 raw를
    재계산하지 않고 여기에 offline 피처를 PIT로 붙인다.
    """
    project = require_bigquery_project()

    from google.cloud import bigquery

    client = bigquery.Client(project=project)
    query = f"""
        SELECT user_id, video_id, event_timestamp, clicked
        FROM `{feature_table_id("training_entity")}`
        WHERE DATE(event_timestamp, 'Asia/Seoul') BETWEEN '{start_date}' AND '{end_date}'
    """
    return client.query(query).to_dataframe()


# MLflow 파라미터 값은 길이 제한이 있고(백엔드마다 상이) 날짜 목록은 요청 기간이 길수록
# 길어진다. 잘라 담되 **잘렸다는 사실을 값 안에 남겨** 목록이 전부인 것처럼 읽히지 않게 한다.
_MAX_LINEAGE_DAY_LIST = 10


def _truncate_day_list(days: tuple[str, ...]) -> str:
    if not days:
        return "none"
    head = ",".join(days[:_MAX_LINEAGE_DAY_LIST])
    rest = len(days) - _MAX_LINEAGE_DAY_LIST
    return head if rest <= 0 else f"{head},+{rest}more"


@dataclass(frozen=True)
class SpineCoverage:
    """요청 기간 대비 spine이 실제로 덮은 날짜 구성(#464).

    학습이 "몇 일치로 돌았는지"를 판정 가능한 형태로 만든다. 행 수만으로는
    하루가 통째로 빠진 것과 모든 날이 조금씩 적은 것을 구분할 수 없다.
    """

    requested_days: tuple[str, ...]
    usable_days: tuple[str, ...]
    sparse_days: tuple[str, ...]
    missing_days: tuple[str, ...]
    zero_click_days: tuple[str, ...]
    total_rows: int
    total_clicks: int
    undated_rows: int = 0

    def describe(self) -> str:
        """사람이 읽고 바로 원인을 알 수 있는 한 줄 요약."""
        undated = f", 날짜 없음 {self.undated_rows:,}행" if self.undated_rows else ""
        return (
            f"요청 {len(self.requested_days)}일 중 사용 가능 {len(self.usable_days)}일 "
            f"(빈 날 {len(self.missing_days)}, 희박한 날 {len(self.sparse_days)}), "
            f"총 {self.total_rows:,}행 / {self.total_clicks:,}클릭{undated}"
        )

    def as_lineage_params(self, *, min_days: int) -> dict[str, str]:
        """MLflow run에 남길 lineage 파라미터로 변환한다(#464 리뷰).

        v12 사고의 본질은 "요청 구간 ≠ 실제 학습 구간인데 메타데이터로는 구분할 수
        없었다"는 것이다. 요청 구간만 기록하면 그 비대칭이 그대로 남으므로, **실측
        커버리지와 적용된 기준**을 함께 남겨 사후에 run만 보고 판별할 수 있게 한다.

        빠진 날짜는 개수만이 아니라 **목록**으로 남긴다 — 개수만으로는 "어느 날이
        빠졌는지"를 알 수 없어 재현·백필 대상을 특정할 수 없다. MLflow 파라미터 값
        길이 제한을 고려해 목록은 잘라 담고, 잘렸으면 그 사실을 표시한다.

        Args:
            min_days: 이 실행에 실제로 적용된 최소 일수 기준. ``0``이면 우회 실행이며,
                그 사실 자체가 lineage에 남아야 정상 실행과 구별된다.
        """
        return {
            "spine_requested_days": str(len(self.requested_days)),
            "spine_usable_days": str(len(self.usable_days)),
            "spine_missing_days": str(len(self.missing_days)),
            "spine_sparse_days": str(len(self.sparse_days)),
            "spine_missing_day_list": _truncate_day_list(self.missing_days),
            "spine_coverage_min_days_applied": str(min_days),
            "spine_coverage_guard": "off" if min_days <= 0 else "on",
        }


def summarize_spine_coverage(
    spine: pd.DataFrame,
    start_date: str,
    end_date: str,
    *,
    min_rows_per_day: int = DEFAULT_MIN_ROWS_PER_DAY,
) -> SpineCoverage:
    """spine을 KST 날짜로 집계해 요청 기간의 커버리지를 만든다(순수 함수, #464).

    BigQuery 없이 단위 테스트 가능하도록 조회와 분리한다. ``min_rows_per_day``
    미만인 날은 "있다"고 세지 않는다 — 2026-07-23/24처럼 유저 10명(240행)만 남은
    붕괴일을 정상일과 같이 세면 커버리지 판정이 무의미해진다.

    ``min_rows_per_day``는 **실행 단위로 조정할 수 없다** — CLI 플래그가 없고
    ``CTR_TRAINING_MIN_ROWS_PER_DAY``(전역)로만 바꾼다. ``min_coverage_days``와
    비대칭인 것은 의도다: 하한 자체는 붕괴일(240행)과 정상일(12만+)을 가르는
    값이라 실행마다 바꿀 이유가 없고, 실행 단위로 완화해야 하는 상황은
    ``--min-coverage-days 0``(검사 전체 우회)으로 충분하다.

    시각이 ``NaT``인 행은 어느 날짜에도 귀속되지 않으므로 ``undated_rows``로
    따로 센다 — 조용히 사라지면 ``total_rows``와 일별 합계가 어긋난 채로 남는다.
    """
    requested = tuple(
        d.strftime("%Y-%m-%d")
        for d in pd.date_range(start=start_date, end=end_date, freq="D")
    )
    if not requested:
        raise ValueError(
            f"요청 기간이 비었습니다: start={start_date!r} end={end_date!r} "
            "(start가 end보다 뒤인지 확인하세요)"
        )

    if spine.empty:
        return SpineCoverage(
            requested_days=requested,
            usable_days=(),
            sparse_days=(),
            missing_days=requested,
            zero_click_days=(),
            total_rows=0,
            total_clicks=0,
        )

    # BigQuery TIMESTAMP는 tz-aware(UTC)로 오지만, 테스트가 naive로 만들 수도 있다.
    # KST 날짜가 파티션 계약(#295)의 기준이므로 어느 쪽이든 KST로 맞춘다.
    ts = pd.to_datetime(spine["event_timestamp"])
    ts = ts.dt.tz_localize("UTC") if ts.dt.tz is None else ts.dt.tz_convert("UTC")
    day = ts.dt.tz_convert("Asia/Seoul").dt.strftime("%Y-%m-%d")

    # NaT는 strftime이 NaN을 내고 groupby가 조용히 버린다. 버린 채로 두면 "총 N행"과
    # 일별 합계가 어긋나므로 개수를 세어 드러낸다(#464 리뷰).
    undated_rows = int(day.isna().sum())

    grouped = spine.groupby(day)["clicked"].agg(["size", "sum"])
    usable, sparse, missing, zero_click = [], [], [], []
    for d in requested:
        if d not in grouped.index:
            missing.append(d)
            continue
        rows = int(grouped.loc[d, "size"])
        clicks = int(grouped.loc[d, "sum"])
        (usable if rows >= min_rows_per_day else sparse).append(d)
        if clicks == 0:
            zero_click.append(d)

    return SpineCoverage(
        requested_days=requested,
        usable_days=tuple(usable),
        sparse_days=tuple(sparse),
        missing_days=tuple(missing),
        zero_click_days=tuple(zero_click),
        total_rows=int(len(spine)),
        total_clicks=int(spine["clicked"].sum()),
        undated_rows=undated_rows,
    )


def require_spine_coverage(
    coverage: SpineCoverage, *, min_days: int = DEFAULT_MIN_COVERAGE_DAYS
) -> None:
    """커버리지가 기준 미달이면 원인을 드러내며 실패시킨다(#464).

    판정은 요청 대비 **비율이 아니라 절대 하한**이다. 30일을 요청해 4일만 확보한
    실행은 이 검사를 통과한다 — 비율 기준을 두면 짧은 윈도우(7일 중 3일 = 43%)와
    긴 윈도우(30일 중 4일 = 13%)에 같은 잣대를 댈 수 없어서다. 대신 결손이 큰
    실행을 사후에 식별할 수 있도록 실측 커버리지를 MLflow lineage에 남긴다
    (``SpineCoverage.as_lineage_params``) — run 파라미터만 보고
    "요청 30일 / 사용 가능 4일"을 판별할 수 있다.

    ``min_days=0``이면 검사를 건너뛴다 — 백필·좁은 구간 재현처럼 의도적으로
    적은 날짜를 쓰는 경우를 막지 않기 위한 명시적 우회구다. 우회한 사실도
    lineage에 ``spine_coverage_guard=off``로 남아 정상 실행과 구별된다.
    """
    if coverage.undated_rows:
        # 어느 날짜에도 귀속되지 않은 행이다. 커버리지 판정에서 빠지므로
        # "총 N행"만 보고 안심하면 안 된다.
        print(
            f"  [경고] event_timestamp가 비어 날짜에 귀속되지 않은 행: "
            f"{coverage.undated_rows:,}행 — 커버리지 집계에서 제외됩니다."
        )

    if min_days <= 0:
        print(
            "  [경고] spine 커버리지 검증이 꺼져 있습니다(min_days<=0). "
            "이 실행은 MLflow lineage에 spine_coverage_guard=off로 기록됩니다."
        )
        return

    if len(coverage.usable_days) < min_days:
        raise ValueError(
            "학습에 쓸 수 있는 날이 부족합니다 — "
            f"{coverage.describe()}. 최소 {min_days}일이 필요합니다.\n"
            f"  요청 기간: {coverage.requested_days[0]} ~ {coverage.requested_days[-1]}\n"
            f"  사용 가능: {list(coverage.usable_days) or '없음'}\n"
            f"  데이터 없음: {list(coverage.missing_days) or '없음'}\n"
            f"  행이 너무 적음: {list(coverage.sparse_days) or '없음'}\n"
            "기간을 넓히거나, 의도한 축소라면 `--min-coverage-days 0`으로 우회하세요"
            "(Python API에서는 min_coverage_days=0)."
        )

    # 클릭 0인 날은 그 자체로 실패는 아니다(다른 날에 양성이 있으면 학습 가능).
    # 다만 val 분할이 그 날에 몰리면 단일 클래스로 지표가 nan이 되므로(#445) 남긴다.
    if coverage.zero_click_days:
        print(
            f"  [경고] 클릭이 0인 날: {list(coverage.zero_click_days)} "
            "— 분할에 따라 지표가 nan이 될 수 있습니다(#445)."
        )


def resolve_extra_feature_columns(
    extra_features: Sequence[str] | None,
) -> tuple[str, ...]:
    """CSV에 보존할 실험 피처 이름을 **비싼 조회 전에** 검증한다(#454).

    ``require_spine_coverage``와 같은 이유로 앞에 세운다 — 이름이 잘못된 조립에
    ``get_historical_features`` 수 분과 BigQuery 스캔을 쓰지 않는다.

    중복과 prod 계약(``MODEL_FEATURE_COLUMNS``) 충돌 판정은 계약 계층
    (``resolve_experiment_feature_columns``)이 이미 소유하므로 여기서 다시 정의하지
    않는다. 조립 고유의 금지만 앞에 둔다: 빈 이름(CSV 컬럼이 될 수 없다)과 라벨
    컬럼 ``clicked``(CSV 마지막에 따로 쓰므로 겹치면 같은 컬럼이 두 번 나간다).

    미지정(None·빈 목록)은 실패가 아니라 prod 경로다 — 기존 22컬럼 계약을 그대로 쓴다.

    Returns:
        검증을 통과한 실험 피처 이름. 미지정이면 빈 튜플.

    Raises:
        FeatureContractError: 이름이 비었거나, ``clicked``거나, 중복이거나, prod 계약과
            겹치면.
    """
    if not extra_features:
        return ()

    # 이름은 여기서 한 번 정규화한다 — 조립과 비교 계층이 서로 다른 문자열을 같은
    # 실험 피처로 취급하면 "선언했는데 없다"는 오진이 난다.
    requested = tuple(name.strip() for name in extra_features)
    if any(not name for name in requested):
        raise FeatureContractError(
            f"실험 피처 이름이 비었습니다: {list(requested)}. "
            "공백만 있는 이름은 CSV 컬럼이 될 수 없습니다."
        )
    if "clicked" in requested:
        raise FeatureContractError(
            "실험 피처로 라벨 컬럼 'clicked'를 지정할 수 없습니다 — "
            "학습 CSV는 라벨을 마지막 컬럼으로 따로 씁니다."
        )
    resolve_experiment_feature_columns(requested)
    return requested


def is_experiment_assembly(
    *,
    feature_service: str | None,
    extra_features: Sequence[str] | None,
) -> bool:
    """이 조립이 prod 기본 조건에서 벗어난 실험 조립인지 판정한다(#530).

    #454가 이 판정을 도입했지만 ``require_explicit_experiment_output`` 안에 갇혀
    있어 다른 곳에서 쓸 수 없었다. 게시 게이팅(#530)이 같은 판정을 필요로 하므로
    predicate로 분리한다 — 조건식이 두 벌이면 ``DEFAULT_SERVICE`` 판별 기준이 바뀔 때
    한쪽만 고치는 실수가 난다.
    """
    from src.features.feast_retrieval import DEFAULT_SERVICE

    experiment_service = (
        feature_service is not None and feature_service != DEFAULT_SERVICE
    )
    return experiment_service or bool(extra_features)


def require_explicit_experiment_output(
    *,
    feature_service: str | None,
    extra_features: Sequence[str] | None,
) -> None:
    """실험 조립이 prod 학습 데이터셋 기본 경로를 덮어쓰지 못하게 막는다(#454).

    prod 경로가 실험 서비스로 조회된 CSV로 바뀌어도 학습은 ``MODEL_FEATURE_COLUMNS``만
    선택하므로 여분 컬럼을 조용히 무시한다 — champion 후보가 실험 데이터로 학습됐다는
    사실이 지표에도, 컬럼 수에도 드러나지 않는다. 그래서 경로를 명시하게 요구한다.
    """
    if not is_experiment_assembly(
        feature_service=feature_service, extra_features=extra_features
    ):
        return
    raise FeatureContractError(
        "실험 조립(--feature-service 또는 --extra-features)은 출력 경로를 명시해야 "
        "합니다 — 기본 경로는 prod 학습 데이터셋이라 덮어쓰면 이후 prod 학습이 실험 "
        "데이터로 조용히 진행됩니다."
    )


def _verify_assembly_environment() -> None:
    """feast 조립에 필요한 환경을 BigQuery 접속 전에 확인한다(#404/#423).

    순서가 중요하다 — BigQuery 클라이언트 생성(load_training_entity_spine)보다
    먼저 실행돼야, 자격증명 없는 환경에서 응답 없이 멈추는 대신(#396/#423 실측)
    즉시 명확한 이유와 함께 실패한다. 검사는 싼 것부터: 환경변수 → GCP 자격증명
    → feast import. 앞의 둘은 환경변수 읽기와 ``os.path.exists`` 몇 번이라
    마이크로초 수준이지만, ``import feast``는 pandas/pyarrow/protobuf까지 끌어와
    실제로 수 초가 걸린다 — 흔한 실패(환경 미설정·미인증)를 더 빨리 되돌려준다.
    """
    missing_env = [
        name for name in ("GCS_REGISTRY_PATH", "GCS_STAGING_LOCATION")
        if not os.environ.get(name)
    ]
    if missing_env:
        raise ValueError(
            f"{', '.join(missing_env)} 환경변수가 필요합니다. .env.example을 참고해 설정하세요."
        )

    # GKE 등 컨테이너 환경은 Workload Identity(metadata server)로 인증하므로
    # 로컬 자격증명 파일이 없어도 정상이다(docs/guides/training-image.md,
    # deploy/feast/apply-job.yaml 확인). KUBERNETES_SERVICE_HOST(모든 k8s pod에
    # 자동 존재)가 있으면 이 체크를 건너뛴다.
    if not os.environ.get("KUBERNETES_SERVICE_HOST"):
        # gcloud/google-auth 모두 CLOUDSDK_CONFIG로 config 디렉토리를 옮길 수 있다
        # (멀티 계정·CI 격리 셋업에서 흔하다) — 하드코딩하면 정상 인증된 개발자를
        # 잘못 막는다.
        gcloud_config_dir = os.environ.get("CLOUDSDK_CONFIG") or os.path.expanduser(
            "~/.config/gcloud"
        )
        adc_path = os.path.join(gcloud_config_dir, "application_default_credentials.json")
        # 환경변수가 "설정만" 된 것으로는 부족하다 — 가리키는 파일이 실제로 있어야
        # 인증이 성립하므로 ADC 경로와 같은 기준(존재 여부)으로 판단한다.
        gac_path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
        gac_valid = bool(gac_path) and os.path.exists(gac_path)
        if not gac_valid and not os.path.exists(adc_path):
            raise ValueError(
                "GCP 자격증명이 감지되지 않습니다 — BigQuery 접속이 응답 없이 멈출 수 "
                "있습니다(#396/#423 실측). `gcloud auth application-default login`을 "
                "실행하거나 GOOGLE_APPLICATION_CREDENTIALS를 설정하세요."
            )

    try:
        import feast  # noqa: F401
    except ImportError as error:
        raise ValueError(
            "feast 패키지가 설치되어 있지 않습니다. dev 그룹과 의존성 충돌로 "
            "격리 그룹입니다 — `uv sync --only-group feast`로 설치하세요."
        ) from error


def _download_pinned_registry(
    uri: str,
    destination: Path,
    *,
    client: object | None = None,
) -> RegistryProvenance:
    """GCS registry object의 현재 generation을 고정해 local file로 내려받는다.

    URI metadata를 먼저 읽고 같은 generation을 지정한 Blob만 다운로드한다. Feast에는
    원격 URI를 전달하지 않고 이 local path를 전달해, manifest가 식별한 registry와 실제
    PIT 조회 registry가 달라지는 경로를 차단한다.
    """
    parsed = urlparse(uri)
    if parsed.scheme != "gs" or not parsed.netloc or not parsed.path.strip("/"):
        raise ProvenanceValidationError(
            f"registry URI는 gs://bucket/object 형식이어야 합니다: {uri}"
        )

    if client is None:
        from google.cloud import storage

        client = storage.Client()

    bucket_name = parsed.netloc
    object_name = parsed.path.lstrip("/")
    try:
        bucket = client.bucket(bucket_name)
        metadata_blob = bucket.blob(object_name)
        metadata_blob.reload()
    except Exception as error:
        raise ProvenanceValidationError(
            f"registry metadata를 읽지 못했습니다: {uri}"
        ) from error

    generation = getattr(metadata_blob, "generation", None)
    if generation is None:
        raise ProvenanceValidationError(f"registry object generation이 없습니다: {uri}")

    try:
        pinned_blob = bucket.blob(object_name, generation=int(generation))
        destination.parent.mkdir(parents=True, exist_ok=True)
        pinned_blob.download_to_filename(str(destination))
        registry_sha256 = sha256_file(destination)
    except Exception as error:
        destination.unlink(missing_ok=True)
        raise ProvenanceValidationError(
            f"registry generation={generation}을 내려받지 못했습니다: {uri}"
        ) from error

    return RegistryProvenance(
        uri=uri,
        generation=str(generation),
        sha256=registry_sha256,
    )


def _assemble_via_feast(
    output_path: str,
    events_start_date: str,
    events_end_date: str,
    *,
    min_coverage_days: int = DEFAULT_MIN_COVERAGE_DAYS,
    feature_service: str | None = None,
    extra_features: Sequence[str] | None = None,
) -> SpineCoverage:
    """Feast get_historical_features(PIT)로 spine에 21피처를 붙여 CSV로 쓴다(#358).

    DuckDB 재계산 경로를 대체한다. offline store가 정본(#357)이라 그 값을 그대로 읽는다.
    feast/feature_repo는 이 경로에서만 필요하므로 지연 import한다(격리 그룹).

    Args:
        min_coverage_days: spine 커버리지 하한(#464). 0이면 검사를 건너뛴다.
        feature_service: 조회할 FeatureService 이름. None이면 prod 기본값
            (``DEFAULT_SERVICE``)이며, 실제로 쓴 이름이 snapshot manifest에 남는다(#454).
        extra_features: prod 계약 뒤에 **보존**할 실험 피처 이름(#454). 학습의
            ``--extra-features``는 데이터셋에 이미 있는 컬럼만 승격하므로, 여기서
            보존하지 않으면 FeatureService에 파생 피처를 더해도 가설이 성립하지 않는다.

    Returns:
        실측 spine 커버리지(#464). 호출부(run-pipeline)가 MLflow lineage에 남겨
        "요청 구간 ≠ 실제 학습 구간"을 사후에 판별할 수 있게 한다.
    """
    project = require_bigquery_project()

    from src.features.feast_retrieval import (
        DEFAULT_SERVICE,
        apply_cold_start_defaults,
        build_offline_feature_store,
        drop_user_dynamic_gap_rows,
        require_extra_feature_columns,
        retrieve_training_features,
    )

    # 이름 검증도 커버리지 검사와 같은 이유로 spine 조회 **전에** 한다(#454).
    service = feature_service or DEFAULT_SERVICE
    experiment_columns = resolve_extra_feature_columns(extra_features)

    print("\n[feast] training_entity spine 로드...")
    spine = load_training_entity_spine(events_start_date, events_end_date)
    print(f"  [OK] spine: {len(spine)} rows")

    # 커버리지 검증은 비싼 조회(get_historical_features) **전에** 한다 — 어차피 실패할
    # 조립에 수 분과 BigQuery 스캔을 쓰지 않기 위해서다(#464).
    coverage = summarize_spine_coverage(spine, events_start_date, events_end_date)
    print(f"  [커버리지] {coverage.describe()}")
    require_spine_coverage(coverage, min_days=min_coverage_days)

    # offline 전용 store: prod feature_store.yaml(Redis)을 로드하지 않고, generation을
    # 고정한 local registry snapshot만 읽어 BigQuery offline을 조회한다(#423).
    registry_uri = os.environ["GCS_REGISTRY_PATH"]
    gcs_staging = os.environ["GCS_STAGING_LOCATION"]
    output = Path(output_path)
    output.parent.mkdir(parents=True, exist_ok=True)

    staged_csv: Path | None = None
    try:
        with TemporaryDirectory(prefix="feast_assemble_") as temporary_dir:
            registry_path = Path(temporary_dir) / "registry.db"
            registry = _download_pinned_registry(registry_uri, registry_path)
            online_db = Path(temporary_dir) / "online.db"
            store = build_offline_feature_store(
                str(registry_path),
                project=project,
                dataset=BIGQUERY_DATASET,
                gcs_staging=gcs_staging,
                online_db_path=str(online_db),
            )
            print(f"\n[feast] get_historical_features(PIT) 조회... (service={service})")
            features = retrieve_training_features(store, spine, service=service)

            missing = [c for c in MODEL_FEATURE_COLUMNS if c not in features.columns]
            if missing:
                raise ValueError(f"feast 조회 결과에 누락된 모델 피처: {missing}")
            # 선언한 실험 피처가 없으면 CSV를 쓰기 **전에** 멈춘다(fail-closed, #454) —
            # 잘린 데이터셋을 저장하면 학습 승격 단계에서야 실패해 조립 비용이 버려진다.
            require_extra_feature_columns(features, experiment_columns, service=service)
            # 평가 전용 패스스루 컬럼도 같은 이유로 CSV를 쓰기 전에 확인한다(#505).
            # 빠뜨리면 grouped 지표를 못 재는 데이터셋이 조용히 만들어지고, 그 사실은
            # 평가 단계에 가서야 드러난다 — 조립 비용을 버리기 전에 여기서 끊는다.
            missing_passthrough = [
                column for column in PASSTHROUGH_COLUMNS if column not in features.columns
            ]
            if missing_passthrough:
                raise FeatureContractError(
                    f"조회 결과에 패스스루 컬럼이 없습니다: {missing_passthrough}. "
                    "이 컬럼은 모델 입력이 아니라 유저 단위 grouped 지표의 그룹 키입니다 "
                    f"(service={service}). spine이 해당 컬럼을 싣고 있는지 확인하십시오."
                )

            # (C) 결손 가시화: UserDynamic 전체 null(ttl 초과·#365 결손)은 채우지 않고 드롭
            # (활동 유저를 "신규 유저"로 위장시키지 않는다). 이 뒤에 남는 null(영상 미발견 등)만
            # 서빙과 같은 cold-start 기본값으로 채운다. 제자리 채움 + 선택 시 추가 copy 안 함(리뷰 OOM).
            n_retrieved = len(features)
            features = drop_user_dynamic_gap_rows(features)
            n_dropped = n_retrieved - len(features)
            features = apply_cold_start_defaults(features)
            features["clicked"] = features["clicked"].astype(int)
            # 관측성(#359 C2 리뷰): validate_events/Step3 통계가 사라진 자리를 최소 지표로 대체한다.
            # 조용한 데이터 급감·전량 드롭을 운영자가 stdout으로 알아채게, 조회→드롭→학습 행 수와
            # click_rate를 남긴다. 학습 행이 0이면 성공으로 조용히 끝내지 않고 경고를 크게 찍는다
            # (하드 실패로 막을지는 후속 판단 — 지금은 실패 의미를 바꾸지 않는다).
            click_rate = float(features["clicked"].mean()) if len(features) else 0.0
            print(
                f"  [관측] 조회 {n_retrieved}행 -> UserDynamic gap 드롭 {n_dropped}행 "
                f"-> 학습 {len(features)}행, click_rate={click_rate:.4f}"
            )
            if features.empty:
                print(
                    "  [경고] 학습 행이 0입니다 — spine이 비었거나 UserDynamic 결손(#365)으로 "
                    "전량 드롭됐습니다. 이어지는 train-model이 빈 데이터로 실패할 수 있습니다."
                )

            with NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                dir=output.parent,
                prefix=f".{output.name}.",
                suffix=".tmp",
                delete=False,
            ) as temporary_file:
                staged_csv = Path(temporary_file.name)
                # 실험 피처는 prod 계약 **뒤·라벨 앞**에 고정 순서로 붙인다(#454).
                # 이 컬럼들의 null은 채우지 않는다 — apply_cold_start_defaults는 prod 계약
                # 컬럼만 다루며, 가설이 더한 컬럼의 결측 의미는 가설 소유자가 정의한다.
                features[
                    [
                        *MODEL_FEATURE_COLUMNS,
                        *experiment_columns,
                        "clicked",
                        *PASSTHROUGH_COLUMNS,
                    ]
                ].to_csv(temporary_file, index=False)
                temporary_file.flush()
                os.fsync(temporary_file.fileno())

            manifest = build_snapshot_manifest(
                dataset_path=staged_csv,
                events_start_date=events_start_date,
                events_end_date=events_end_date,
                feature_service=service,
                registry=registry,
                code_archive_sha=os.environ.get("CODE_ARCHIVE_SHA"),
            )
            os.replace(staged_csv, output)
            staged_csv = None
            write_manifest_atomic(manifest, snapshot_manifest_path(output))

    finally:
        if staged_csv is not None:
            staged_csv.unlink(missing_ok=True)

    experiment_note = f", 실험 피처 {list(experiment_columns)}" if experiment_columns else ""
    print(
        f"\n[저장] {output} ({len(features)} rows, feast 경로 + snapshot provenance"
        f", service={service}{experiment_note})"
    )
    return coverage


def derive_wide_events(
    long_events: pd.DataFrame,
    label_window_sec: int = LABEL_WINDOW_SEC,
    followup_window_sec: int = FOLLOWUP_WINDOW_SEC,
) -> pd.DataFrame:
    """long-format(impression/click/view/like) 이벤트를 wide-format(행당
    event_id/user_id/video_id/timestamp/clicked/liked/watch_time_sec)으로
    변환한다. 순수 함수라 BigQuery 없이 단위 테스트 가능하다.

    Attribution 규칙:
    - click 귀속: 같은 (user_id, video_id), click **직전** label_window_sec
      이내 **가장 가까운(최근)** impression에 귀속(ORDER BY 시각 DESC).
    - 한 impression에 click 후보가 여러 개 매칭되면 **가장 이른 click을
      anchor로 고정**한다(ORDER BY click 시각 ASC) — 이후 view/like 체이닝은
      이 anchor 하나로만 진행한다.
    - view 귀속: anchor click **이후** followup_window_sec 이내 **가장
      먼저 발생한** view(ORDER BY 시각 ASC, click 기준).
    - like 귀속: click이 아니라 **확정된 view 이후** followup_window_sec
      이내 가장 먼저 발생한 like(view 기준 순차 체인 — 실제 생성기의
      like_ts = view_ts + α 인과관계와 동일). **view가 없으면 like도
      항상 0**이다(view를 거치지 않는 독립 탐색은 하지 않는다).
    - click이 없는 impression(대다수)은 clicked=liked=0, watch_time_sec=0.

    이 규칙 중 click 귀속(label_window_sec)만 docs/guides/data-warehouse.md의
    training_entity에 문서화되어 있고, view/like 체이닝(followup_window_sec)은
    이번에 새로 정의한 규칙이라 같은 문서에 추가 반영한다.
    """
    con = connect_duckdb()
    # 빈 파티션(콜드 스타트)에서는 BigQuery가 STRING 컬럼을 object dtype 빈
    # 컬럼으로 반환해 DuckDB가 타입을 추론하지 못하고 INTEGER로 등록한다 —
    # 이후 문자열 키 비교가 깨지므로 등록 전에 계약 dtype을 고정한다.
    long_events = long_events.astype(
        {"event_id": "string", "user_id": "string", "video_id": "string", "event_type": "string"}
    )
    con.register("long_events", long_events)

    query = f"""
        WITH impressions AS (
            SELECT event_id, event_timestamp, user_id, video_id
            FROM long_events WHERE event_type = 'impression'
        ),
        clicks AS (
            SELECT event_id, event_timestamp, user_id, video_id
            FROM long_events WHERE event_type = 'click'
        ),
        views AS (
            SELECT event_id, event_timestamp, user_id, video_id, watch_time_sec
            FROM long_events WHERE event_type = 'view'
        ),
        likes AS (
            SELECT event_id, event_timestamp, user_id, video_id
            FROM long_events WHERE event_type = 'like'
        ),
        click_attr AS (
            SELECT
                c.event_id AS click_event_id,
                c.event_timestamp AS click_ts,
                c.user_id,
                c.video_id,
                i.event_id AS impression_event_id
            FROM clicks c
            JOIN impressions i
                ON i.user_id = c.user_id AND i.video_id = c.video_id
               AND i.event_timestamp < c.event_timestamp
               AND i.event_timestamp >= c.event_timestamp - INTERVAL {label_window_sec} SECOND
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY c.event_id ORDER BY i.event_timestamp DESC
            ) = 1
        ),
        impression_click AS (
            -- 한 impression에 click 후보가 여러 개면 가장 이른 click을 anchor로 고정
            SELECT impression_event_id, click_event_id, click_ts, user_id, video_id
            FROM click_attr
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY impression_event_id ORDER BY click_ts ASC
            ) = 1
        ),
        view_attr AS (
            SELECT
                ic.impression_event_id,
                v.event_timestamp AS view_ts,
                v.watch_time_sec
            FROM impression_click ic
            JOIN views v
                ON v.user_id = ic.user_id AND v.video_id = ic.video_id
               AND v.event_timestamp > ic.click_ts
               AND v.event_timestamp <= ic.click_ts + INTERVAL {followup_window_sec} SECOND
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY ic.impression_event_id ORDER BY v.event_timestamp ASC
            ) = 1
        ),
        like_attr AS (
            -- like는 click이 아니라 "확정된 view" 이후로만 체이닝한다(순차 인과관계).
            -- view가 없으면 이 CTE에 해당 impression이 아예 안 나타나므로 liked=0.
            SELECT va.impression_event_id
            FROM view_attr va
            JOIN impression_click ic ON ic.impression_event_id = va.impression_event_id
            JOIN likes l
                ON l.user_id = ic.user_id AND l.video_id = ic.video_id
               AND l.event_timestamp > va.view_ts
               AND l.event_timestamp <= va.view_ts + INTERVAL {followup_window_sec} SECOND
            QUALIFY ROW_NUMBER() OVER (
                PARTITION BY va.impression_event_id ORDER BY l.event_timestamp ASC
            ) = 1
        )
        SELECT
            i.event_id,
            i.user_id,
            i.video_id,
            i.event_timestamp AS timestamp,
            CASE WHEN ic.click_event_id IS NOT NULL THEN 1 ELSE 0 END AS clicked,
            CASE WHEN la.impression_event_id IS NOT NULL THEN 1 ELSE 0 END AS liked,
            CAST(COALESCE(va.watch_time_sec, 0) AS BIGINT) AS watch_time_sec
        FROM impressions i
        LEFT JOIN impression_click ic ON ic.impression_event_id = i.event_id
        LEFT JOIN view_attr va ON va.impression_event_id = i.event_id
        LEFT JOIN like_attr la ON la.impression_event_id = i.event_id
    """
    wide = con.execute(query).df()
    # 빈 결과도 하류(DuckDB 재등록)에서 dtype이 보존되도록 문자열 계약을 명시한다.
    return wide.astype({"event_id": "string", "user_id": "string", "video_id": "string"})


def main(
    output_path: str = None,
    events_start_date: str = None,
    events_end_date: str = None,
    min_coverage_days: int = DEFAULT_MIN_COVERAGE_DAYS,
    *,
    feature_service: str | None = None,
    extra_features: Sequence[str] | None = None,
) -> SpineCoverage:
    """training_dataset.csv를 offline feature store(Feast PIT) 조회로 생성한다(#359 C2, feast-only).

    #359 C2에서 DuckDB 재계산 경로를 제거하고 feast를 유일 경로로 만들었다. spine
    (``training_entity``)에 21피처를 ``get_historical_features``(PIT)로 붙여 CSV로 쓴다
    (``_assemble_via_feast``). offline store가 정본(#357)이라 그 값을 그대로 읽는다.

    Args:
        feature_service: 조회할 FeatureService 이름(기본 ``ctr_training_v1``, #454).
        extra_features: 학습 CSV에 함께 보존할 실험 피처 이름(#454).

    실험 조립(기본이 아닌 FeatureService 또는 실험 피처)은 ``output_path``를 명시해야
    한다. 기본 경로는 prod 학습 데이터셋이라, 실험 조립이 그 자리를 덮어쓰면 이후 prod
    학습이 실험 서비스로 조회된 데이터를 쓰면서도 컬럼 수만 맞아 조용히 성공한다.

    Returns:
        실측 spine 커버리지(#464). ``build-features``는 쓰지 않지만 ``run-pipeline``이
        MLflow lineage에 남긴다.

    Raises:
        FeatureContractError: 실험 조립인데 ``output_path``를 지정하지 않으면.
    """
    if not events_start_date or not events_end_date:
        raise ValueError(
            "events_start_date/events_end_date가 필요합니다 "
            "(spine=training_entity를 BQ에서 KST 날짜 폐구간으로 조회한다)"
        )
    require_bigquery_project()
    _verify_assembly_environment()
    if output_path is None:
        require_explicit_experiment_output(
            feature_service=feature_service, extra_features=extra_features
        )
        output_path = os.path.join(get_data_dir(), "processed", "training_dataset.csv")
    return _assemble_via_feast(
        output_path,
        events_start_date,
        events_end_date,
        min_coverage_days=min_coverage_days,
        feature_service=feature_service,
        extra_features=extra_features,
    )
