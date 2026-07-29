#!/usr/bin/env python3
"""training_dataset.csv 생성 파이프라인 — offline feature store PIT 조회 (#359 C2로 feast-only).

[파이프라인] 피처 구간 — spine(``training_entity``, KST 날짜 폐구간 [start, end])에 21개 모델
피처를 Feast ``get_historical_features``(point-in-time)로 붙여 CSV로 쓴다(``_assemble_via_feast``).
#359 C2에서 DuckDB 재계산 경로(raw에서 자체 계산)를 제거하고 feast를 **유일 경로**로 만들었다 —
offline store가 정본(#357)이라 그 값을 그대로 읽는다.

출력: data/processed/training_dataset.csv (21 모델 피처 + ``clicked`` label = 22 물리 컬럼).
model input의 이름·순서·categorical 분류는 ``src/features/model_contract.py``가, staged PIT 조회는
``src/features/feast_retrieval.py``가 소유한다(이 모듈은 재정의하지 않는다).

[비책임] 학습 조립(feast)이 쓰지 않지만 인접 소비자와 공유하느라 이 모듈에 남은 헬퍼:
``derive_wide_events``(long→wide attribution)·``load_events_from_bigquery``는 일일추천
(``daily_recommendations``)이, ``load_personas``(+ ``to_personas_frame`` import)는 정책 시뮬레이션
(``simulate_policy_round``)과 벤치(``scripts/bench/bench_feature_assembly.py``)가 쓴다 —
이들의 feast 전환(#359 C3)에서 함께 정리한다. DuckDB 재계산·mock CSV 입력 경로는 #359 C2에서 제거됐다.
"""

import os
import sys

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.features.assembly import connect_duckdb  # noqa: E402
from src.features.model_contract import MODEL_FEATURE_COLUMNS  # noqa: E402
from src.pipeline.virtual_user_adapter import to_personas_frame  # noqa: E402

BIGQUERY_PROJECT = os.environ.get("CTR_TRAINING_BQ_PROJECT", "ar-infra-501607")
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


def raw_table_id(table: str) -> str:
    """raw(데이터 레이크) 테이블의 완전한 BigQuery 식별자를 만든다.

    raw 테이블(`data_lake_*`)은 feature 계층과 다른 dataset
    (`CTR_TRAINING_BQ_RAW_DATASET`, 기본 `data_lake_raw`)에 있다. 모듈
    전역을 호출 시점에 읽으므로 테스트에서 monkeypatch 로 재정의할 수 있다.
    """
    return f"{BIGQUERY_PROJECT}.{BIGQUERY_RAW_DATASET}.{table}"


def feature_table_id(table: str) -> str:
    """feature/서빙 테이블의 완전한 BigQuery 식별자를 만든다.

    Feast feature 테이블과 배치 출력 테이블은 계속
    `CTR_TRAINING_BQ_DATASET`(기본 `feast_offline_store`)에 있다.
    """
    return f"{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}.{table}"


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
    from google.cloud import bigquery

    client = bigquery.Client(project=BIGQUERY_PROJECT)
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
    from google.cloud import bigquery

    client = bigquery.Client(project=BIGQUERY_PROJECT)
    query = f"""
        SELECT user_id, video_id, event_timestamp, clicked
        FROM `{feature_table_id("training_entity")}`
        WHERE DATE(event_timestamp, 'Asia/Seoul') BETWEEN '{start_date}' AND '{end_date}'
    """
    return client.query(query).to_dataframe()


def _assemble_via_feast(
    output_path: str, events_start_date: str, events_end_date: str
) -> None:
    """Feast get_historical_features(PIT)로 spine에 21피처를 붙여 CSV로 쓴다(#358).

    DuckDB 재계산 경로를 대체한다. offline store가 정본(#357)이라 그 값을 그대로 읽는다.
    feast/feature_repo는 이 경로에서만 필요하므로 지연 import한다(격리 그룹).
    """
    import tempfile

    from src.features.feast_retrieval import (
        apply_cold_start_defaults,
        build_offline_feature_store,
        drop_user_dynamic_gap_rows,
        retrieve_training_features,
    )

    print("\n[feast] training_entity spine 로드...")
    spine = load_training_entity_spine(events_start_date, events_end_date)
    print(f"  [OK] spine: {len(spine)} rows")

    # offline 전용 store: prod feature_store.yaml(Redis)을 로드하지 않고, 배포 job이 apply한
    # prod 레지스트리(GCS_REGISTRY_PATH)만 읽어 BigQuery offline을 조회한다(#358 리뷰).
    registry_path = os.environ["GCS_REGISTRY_PATH"]
    gcs_staging = os.environ["GCS_STAGING_LOCATION"]
    online_db = os.path.join(tempfile.mkdtemp(prefix="feast_assemble_"), "online.db")
    store = build_offline_feature_store(
        registry_path,
        project=BIGQUERY_PROJECT,
        dataset=BIGQUERY_DATASET,
        gcs_staging=gcs_staging,
        online_db_path=online_db,
    )
    print("\n[feast] get_historical_features(PIT) 조회...")
    features = retrieve_training_features(store, spine)

    missing = [c for c in MODEL_FEATURE_COLUMNS if c not in features.columns]
    if missing:
        raise ValueError(f"feast 조회 결과에 누락된 모델 피처: {missing}")

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
    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    features[[*MODEL_FEATURE_COLUMNS, "clicked"]].to_csv(output_path, index=False)
    print(f"\n[저장] {output_path} ({len(features)} rows, feast 경로)")


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
):
    """training_dataset.csv를 offline feature store(Feast PIT) 조회로 생성한다(#359 C2, feast-only).

    #359 C2에서 DuckDB 재계산 경로를 제거하고 feast를 유일 경로로 만들었다. spine
    (``training_entity``)에 21피처를 ``get_historical_features``(PIT)로 붙여 CSV로 쓴다
    (``_assemble_via_feast``). offline store가 정본(#357)이라 그 값을 그대로 읽는다.
    """
    if not events_start_date or not events_end_date:
        raise ValueError(
            "events_start_date/events_end_date가 필요합니다 "
            "(spine=training_entity를 BQ에서 KST 날짜 폐구간으로 조회한다)"
        )
    if output_path is None:
        output_path = os.path.join(get_data_dir(), "processed", "training_dataset.csv")
    _assemble_via_feast(output_path, events_start_date, events_end_date)
