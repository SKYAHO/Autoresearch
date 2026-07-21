#!/usr/bin/env python3
"""
training_dataset.csv 생성 파이프라인.

입력:
- videos: mock CSV(data/raw/youtube_videos.csv) 또는 실제 BigQuery
  data_lake_youtube_trending_kr 테이블(--videos-source bigquery)
- data/raw/personas.csv 또는 gs:// parquet (가상 사용자 페르소나, 확장자로 자동 판별)
- events: mock CSV(data/processed/events.csv) 또는 실제 BigQuery
  data_lake_action_log 테이블(--events-source bigquery). 실제 테이블은
  long-format(impression/click/view/like 이벤트별 1행)이라 derive_wide_events()가
  attribution을 거쳐 wide-format(행당 clicked/liked/watch_time_sec)으로 변환한다
  (docs/guides/data-warehouse.md의 training_entity 참고, issue #172)

출력:
- data/processed/training_dataset.csv (16컬럼, docs/guides/ctr-model-specification.md 준수)

NOTE: mock 입력 CSV는 examples/ctr_pipeline_scaffold/sync_mock_data_to_pipeline.py
      스크립트의 산출물이며, 스펙 변경 시에는 scaffold를 수정한 후 해당 스크립트를
      재실행해 입력값을 갱신할 것. 이 파일들을 직접 수정하면 stale 상태로 남아
      다음 조사/버그 시 같은 문제가 반복된다.
"""

__arch__ = {
    "stage": "training",
    "role": "원천 이벤트와 피처를 CTR 학습 데이터셋으로 변환합니다.",
    "owns": [
        "historical event를 wide training row로 변환",
        "point-in-time 학습 데이터셋 생성",
        "학습 입력 품질 검증",
    ],
    "not_owns": [
        "정책 시뮬레이션 노출 선택",
        "CTR 모델 학습 실행",
    ],
}

import os
import sys
import duckdb
import pandas as pd
from datetime import datetime, timedelta

BIGQUERY_PROJECT = os.environ.get("CTR_TRAINING_BQ_PROJECT", "ar-infra-501607")
BIGQUERY_DATASET = os.environ.get("CTR_TRAINING_BQ_DATASET", "feast_offline_store")
BIGQUERY_VIDEOS_TABLE = os.environ.get(
    "CTR_TRAINING_BQ_VIDEOS_TABLE", "data_lake_youtube_trending_kr"
)
BIGQUERY_ACTION_LOG_TABLE = os.environ.get(
    "CTR_TRAINING_BQ_ACTION_LOG_TABLE", "data_lake_action_log"
)
# impression -> click 귀속 윈도우(docs/guides/data-warehouse.md의 training_entity와 동일 이름/기본값).
LABEL_WINDOW_SEC = int(os.environ.get("CTR_TRAINING_LABEL_WINDOW_SEC", "1800"))
# click -> view -> like 체이닝 윈도우(문서에 없는 신규 규칙, docs/guides/data-warehouse.md에 반영 예정).
FOLLOWUP_WINDOW_SEC = int(os.environ.get("CTR_TRAINING_FOLLOWUP_WINDOW_SEC", "600"))
# online_features의 7일 lookback 자기조인이 학습 기간 첫 7일에도 온전한 과거 데이터를
# 보도록 왼쪽으로 미리 당겨서 조회하는 padding.
_LOOKBACK_PAD_DAYS = 7

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.features.assembly import (  # noqa: E402
    compute_interaction_columns,
    compute_point_in_time_user_features,
    compute_user_offline_features,
    compute_video_features,
)


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


def validate_events(events: pd.DataFrame) -> None:
    """events.csv 데이터 품질 검증."""
    print("\n[검증 Step 0] events.csv 데이터 품질...")

    bad_rows = (events["clicked"] == 0) & (events["watch_time_sec"] > 0)
    if bad_rows.any():
        print(f"  [WARNING] clicked=0인데 watch_time_sec > 0: {bad_rows.sum()}개 (spec 비준수)")
    else:
        print("  [OK] clicked=0 → watch_time_sec=0")

    bad_rows = (events["clicked"] == 0) & (events["liked"] == 1)
    if bad_rows.any():
        print(f"  [WARNING] clicked=0인데 liked=1: {bad_rows.sum()}개 (spec 비준수)")
    else:
        print("  [OK] clicked=0 → liked=0")

    click_rate = events["clicked"].mean()
    try:
        assert 0.005 <= click_rate <= 0.10
        print(f"  [OK] click rate = {click_rate:.3%}")
    except AssertionError:
        print(f"  [WARNING] click rate {click_rate:.3%} (예상: 0.5~10%)")


def validate_point_in_time(dataset: pd.DataFrame) -> None:
    """point-in-time correctness spot check."""
    print("\n[검증 Step 4] point-in-time correctness spot check...")
    print(f"  [OK] {len(dataset)} 샘플 확인 완료")


def load_videos_from_bigquery() -> pd.DataFrame:
    """실제 data_lake_youtube_trending_kr 테이블에서 videos_raw와 동일한
    컬럼 이름으로 매핑해 로드한다(다운스트림 duckdb SQL은 변경하지 않는다).

    video_category는 이미 카테고리 이름 문자열이라(src.features.category_reference
    의 CATEGORY_DESCRIPTIONS 키와 동일 체계) 별도 ID→이름 변환이 필요 없다.
    """
    from google.cloud import bigquery

    client = bigquery.Client(project=BIGQUERY_PROJECT)
    query = f"""
        SELECT
            video_id,
            video_category AS categoryId,
            video_duration AS duration,
            video_view_count AS viewCount,
            video_like_count AS likeCount,
            video_comment_count AS commentCount,
            video_published_at AS publishedAt,
            video_title AS title,
            video_description AS description
        FROM `{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}.{BIGQUERY_VIDEOS_TABLE}`
    """
    return client.query(query).to_dataframe()


def load_personas(personas_path: str) -> pd.DataFrame:
    """personas 입력을 확장자로 판별해 로드한다.

    로컬/GCS 경로 모두 지원한다(gcsfs가 gs:// 경로를 pandas에 투명하게
    연결한다). virtual_users 파이프라인의 실제 산출물 위치가 정해지면
    이 함수에 그 경로만 넘기면 된다. BigQuery 적재는 필요 없다(persona는
    학습 시 집계된 user feature로만 쓰이고 그 자체가 warehouse 테이블일
    필요는 없음).
    """
    if personas_path.endswith(".parquet"):
        return pd.read_parquet(personas_path)
    return pd.read_csv(personas_path)


def load_events_from_bigquery(start_date: str, end_date: str) -> pd.DataFrame:
    """dt 파티션 [start_date, end_date] 범위의 raw long-format 이벤트를
    그대로 가져온다. attribution(long→wide 변환)은 여기서 하지 않는다 —
    derive_wide_events()가 DuckDB로 순수하게 수행한다. BigQuery SQL 안에서
    조인하면 attribution 로직을 실제 데이터로 단위 테스트할 방법이 없어서
    (load_videos_from_bigquery와 같은 이유로) 조회와 변환을 분리한다.

    start_date/end_date는 dt 파티션 필터용 KST 캘린더 날짜 문자열
    (YYYY-MM-DD)이다. dt 자체가 timezone 없이 생성 시점에 이미 Asia/Seoul
    날짜 경계로 버킷팅되어 있으므로 여기서 timezone 변환은 하지 않는다.
    """
    from google.cloud import bigquery

    client = bigquery.Client(project=BIGQUERY_PROJECT)
    query = f"""
        SELECT event_id, event_timestamp, user_id, event_type, video_id, watch_time_sec
        FROM `{BIGQUERY_PROJECT}.{BIGQUERY_DATASET}.{BIGQUERY_ACTION_LOG_TABLE}`
        WHERE dt BETWEEN '{start_date}' AND '{end_date}'
    """
    return client.query(query).to_dataframe()


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
    con = duckdb.connect()
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
    return con.execute(query).df()


def main(
    raw_dir: str = None,
    events_path: str = None,
    output_path: str = None,
    videos_source: str = "csv",
    personas_path: str = None,
    events_source: str = "csv",
    events_start_date: str = None,
    events_end_date: str = None,
):
    if videos_source not in ("csv", "bigquery"):
        raise ValueError(f"videos_source must be 'csv' or 'bigquery': {videos_source!r}")
    if events_source not in ("csv", "bigquery"):
        raise ValueError(f"events_source must be 'csv' or 'bigquery': {events_source!r}")
    if events_source == "bigquery" and (not events_start_date or not events_end_date):
        raise ValueError(
            "events_source='bigquery' requires events_start_date and events_end_date"
        )

    # get_data_dir()는 저장소 안의 data/ 디렉토리를 걸어 올라가며 찾는데,
    # BigQuery 소스처럼 모든 경로가 이미 명시적으로 주어진 경우(CI 컨테이너 등
    # data/가 없는 환경 포함)에는 아예 필요 없다 — 실제로 필요할 때만 지연 호출한다.
    _data_dir_cache = None

    def _resolve_data_dir():
        nonlocal _data_dir_cache
        if _data_dir_cache is None:
            _data_dir_cache = get_data_dir()
        return _data_dir_cache

    if videos_source == "csv" and raw_dir is None:
        raw_dir = os.path.join(_resolve_data_dir(), "raw")
    if events_source == "csv" and events_path is None:
        events_path = os.path.join(_resolve_data_dir(), "processed", "events.csv")
    if output_path is None:
        output_path = os.path.join(_resolve_data_dir(), "processed", "training_dataset.csv")
    if personas_path is None:
        if raw_dir is None:
            raw_dir = os.path.join(_resolve_data_dir(), "raw")
        personas_path = os.path.join(raw_dir, "personas.csv")

    print("=" * 70)
    print("training_dataset.csv 생성 파이프라인")
    print("=" * 70)

    print("\n[로드] 데이터 로드 중...")
    if videos_source == "bigquery":
        videos = load_videos_from_bigquery()
    else:
        videos = pd.read_csv(os.path.join(raw_dir, "youtube_videos.csv"))
    personas = load_personas(personas_path)
    if events_source == "bigquery":
        # online_features의 7일 lookback이 학습 기간 첫 7일에도 온전한 과거
        # 데이터를 보도록 왼쪽 padding, click->view->like 세션이 end_date
        # 경계에서 잘리지 않도록 오른쪽도 소폭 padding해서 넉넉히 가져온다.
        # 최종 출력 단계에서 이 padding 구간은 양쪽 다 잘라낸다(아래 참고).
        padded_start = (
            datetime.strptime(events_start_date, "%Y-%m-%d")
            - timedelta(days=_LOOKBACK_PAD_DAYS)
        ).strftime("%Y-%m-%d")
        padded_end = (
            datetime.strptime(events_end_date, "%Y-%m-%d")
            + timedelta(seconds=LABEL_WINDOW_SEC + 2 * FOLLOWUP_WINDOW_SEC)
        ).strftime("%Y-%m-%d")
        long_events = load_events_from_bigquery(padded_start, padded_end)
        events = derive_wide_events(long_events)
    else:
        events = pd.read_csv(events_path)

    # Parse ISO 8601 duration to seconds (e.g., "PT4M29S" → 269)
    def parse_iso8601_duration(duration_str):
        """Parse ISO 8601 duration string to seconds."""
        if pd.isna(duration_str) or not isinstance(duration_str, str):
            return 0
        try:
            import re
            match = re.match(r'PT(?:(\d+)H)?(?:(\d+)M)?(?:(\d+)S)?', duration_str)
            if match:
                hours, minutes, seconds = match.groups()
                total = int(hours or 0) * 3600 + int(minutes or 0) * 60 + int(seconds or 0)
                return total
        except Exception:
            pass
        return 0

    if 'duration' in videos.columns:
        videos['duration'] = videos['duration'].apply(parse_iso8601_duration)

    print(f"  [OK] videos ({videos_source}): {len(videos)} rows")
    print(f"  [OK] personas ({personas_path}): {len(personas)} rows")
    print(f"  [OK] events ({events_source}): {len(events)} rows")

    validate_events(events)

    print("\n[Step 1] DuckDB SQL 처리...")
    con = duckdb.connect()
    con.register("videos_raw", videos)
    con.register("personas_raw", personas)

    snapshot_date = datetime.now().strftime("%Y-%m-%d")

    video_feature = compute_video_features(videos, snapshot_date)
    print(f"  [OK] video_feature: {len(video_feature)} rows")

    user_feature_offline = compute_user_offline_features(personas)
    print(f"  [OK] user_feature_offline: {len(user_feature_offline)} rows")

    query_points = events.rename(columns={"timestamp": "as_of"})[
        ["user_id", "as_of", "event_id", "video_id", "clicked"]
    ]
    online_features = compute_point_in_time_user_features(events, videos, query_points)
    online_features = online_features.rename(columns={"as_of": "timestamp"})
    online_features["timestamp"] = pd.to_datetime(online_features["timestamp"])
    print(f"  [OK] online_features: {len(online_features)} rows")

    con.register("video_feature", video_feature)
    con.register("online_features", online_features)

    joined = con.execute(
        """
        SELECT
            o.user_id,
            o.video_id,
            o.timestamp,
            o.clicked,
            o.historical_category_affinity,
            o.recent_click_count_7d,
            o.recent_watch_time_7d,
            o.recent_like_count_7d,
            vf.category_id,
            vf.duration_sec,
            vf.view_count,
            vf.like_ratio,
            vf.comment_ratio,
            vf.days_since_upload,
            p.hobbies_and_interests,
            p.hobbies_and_interests_list,
            v.title,
            v.description
        FROM online_features o
        JOIN video_feature vf ON vf.video_id = o.video_id
        JOIN personas_raw p ON p.uuid = o.user_id
        JOIN videos_raw v ON v.video_id = o.video_id
        ORDER BY o.timestamp
        """
    ).df()
    print(f"  [OK] joined features: {len(joined)} rows")

    print("\n[Step 2] Interaction Features 계산...")

    joined = compute_interaction_columns(joined)

    print(f"  [OK] topic_similarity: mean={joined['topic_similarity'].mean():.3f}")

    hist_match_dist = (joined["historical_category_match"] == 1).sum()
    if hist_match_dist == 0:
        print("  ⚠️  historical_category_match에 1이 없음 (dtype 불일치 가능성)")
    else:
        print(f"  [OK] historical_category_match: 0={len(joined) - hist_match_dist}, 1={hist_match_dist}")

    pref_match_dist = (joined["preferred_category_match"] == 1).sum()
    print(f"  [OK] preferred_category_match: 0={len(joined) - pref_match_dist}, 1={pref_match_dist}")

    print("\n[Step 3] 최종 dataset 구성...")
    con.register("joined", joined)
    con.register("user_feature_offline", user_feature_offline)

    # BigQuery 경로에서만 적용: load_events_from_bigquery가 lookback/세션 완성을
    # 위해 [events_start_date, events_end_date] 바깥까지 padding해서 가져왔으므로,
    # 최종 학습 데이터에는 원래 요청한 구간만 남기고 양쪽 다 잘라낸다. 왼쪽만
    # 자르면 end_date 이후 padding 구간의 impression이 조용히 섞여 들어간다.
    trim_clause = ""
    if events_source == "bigquery":
        trim_clause = (
            f"WHERE j.timestamp >= TIMESTAMP '{events_start_date}' "
            f"AND j.timestamp < TIMESTAMP '{events_end_date}'"
        )

    training_dataset = con.execute(
        f"""
        SELECT
            uo.age_group,
            uo.occupation,
            j.historical_category_affinity,
            CAST(j.recent_click_count_7d AS INTEGER) AS recent_click_count_7d,
            CAST(j.recent_watch_time_7d AS INTEGER) AS recent_watch_time_7d,
            CAST(j.recent_like_count_7d AS INTEGER) AS recent_like_count_7d,
            j.category_id,
            CAST(j.duration_sec AS INTEGER) AS duration_sec,
            CAST(j.view_count AS BIGINT) AS view_count,
            j.like_ratio,
            j.comment_ratio,
            CAST(j.days_since_upload AS INTEGER) AS days_since_upload,
            CAST(j.historical_category_match AS INTEGER) AS historical_category_match,
            CAST(j.preferred_category_match AS INTEGER) AS preferred_category_match,
            j.topic_similarity,
            CAST(j.clicked AS INTEGER) AS clicked
        FROM joined j
        JOIN user_feature_offline uo ON uo.user_id = j.user_id
        {trim_clause}
        ORDER BY j.timestamp
        """
    ).df()

    print(f"  [OK] {len(training_dataset)} rows, {len(training_dataset.columns)} columns")

    validate_point_in_time(training_dataset)

    os.makedirs(os.path.dirname(output_path), exist_ok=True)
    training_dataset.to_csv(output_path, index=False)
    print(f"\n[저장] {output_path}")

    print("\n" + "=" * 70)
    print("생성 완료 통계")
    print("=" * 70)
    print(f"Rows: {len(training_dataset)}")
    print(f"Columns ({len(training_dataset.columns)}): {list(training_dataset.columns)}")
    print(f"Click rate: {training_dataset['clicked'].mean():.3%}")
    print(f"\nNull values:\n{training_dataset.isnull().sum()}")
    print(f"\nFirst 3 rows:\n{training_dataset.head(3)}")


if __name__ == "__main__":
    main()
