"""학습·시뮬레이션 공용 피처 조립 함수.

build_training_dataset.main()의 인라인 DuckDB SQL과 interaction 계산을 추출한
것이다. 학습 데이터셋 생성과 정책 시뮬레이션 라운드(simulate_policy_round)가
같은 코드로 피처를 계산해 학습-서빙 스큐를 방지한다.
"""

import json
from datetime import datetime

import duckdb
import pandas as pd

from src.features.category_reference import CATEGORY_DESCRIPTIONS
from src.features.embeddings import embed_texts
from src.features.feature_builder import (
    compute_historical_category_match,
    compute_preferred_category_match,
    compute_topic_similarity,
)

KEYWORD_TO_CATEGORY = {
    "gaming": "Gaming",
    "game": "Gaming",
    "music": "Music",
    "sports": "Sports",
    "travel": "Travel & Events",
    "food": "Howto & Style",
    "beauty": "Howto & Style",
    "fashion": "Howto & Style",
    "education": "Education",
    "technology": "Science & Technology",
    "news": "News & Politics",
    "entertainment": "Entertainment",
    "comedy": "Comedy",
    "pet": "Pets & Animals",
    "animal": "Pets & Animals",
}

assert set(KEYWORD_TO_CATEGORY.values()) <= set(CATEGORY_DESCRIPTIONS), \
    f"KEYWORD_TO_CATEGORY has invalid categories: {set(KEYWORD_TO_CATEGORY.values()) - set(CATEGORY_DESCRIPTIONS)}"


def derive_preferred_category(keywords) -> list:
    """FALLBACK MOCK: 키워드 리스트에서 선호 카테고리 파생.

    personas 원본에 virtual_users 파이프라인이 LLM으로 직접 산출한
    primary_categories 컬럼이 없을 때만(구식 mock personas.csv 등) 쓰는
    fallback이다. primary_categories가 있으면 parse_primary_categories()가
    그 실제 값을 그대로 쓴다 (#205, autoresearch/virtual_users/schema.py의
    YouTubeProfile.primary_categories 참고).

    Args:
        keywords: preferred_topics의 키워드 리스트 (영어 또는 한글).

    Returns:
        매핑되는 category_id 리스트 (최대 3개, dedup, 순서 유지).
    """
    categories = []
    seen = set()
    for kw in keywords:
        kw_lower = str(kw).lower()
        if kw_lower in KEYWORD_TO_CATEGORY:
            cat_id = KEYWORD_TO_CATEGORY[kw_lower]
            if cat_id not in seen:
                categories.append(cat_id)
                seen.add(cat_id)
                if len(categories) >= 3:
                    break
    return categories if categories else ["People & Blogs"]


def parse_primary_categories(value) -> list:
    """virtual_users 파이프라인이 산출한 primary_categories 원본을 파싱한다.

    parquet에서 로드하면 파이썬 list로, mock CSV에서 로드하면 JSON 문자열로
    들어온다. docs/guides/data-warehouse.md의 user_static_feature 규칙과
    동일하게 null/빈 값은 빈 리스트로 처리하고, CATEGORY_DESCRIPTIONS
    vocabulary 밖의 값은 걸러낸다(LLM vocab drift 방어).
    """
    if isinstance(value, list):
        categories = value
    elif value is None or pd.isna(value):
        return []
    else:
        try:
            categories = json.loads(str(value))
        except (json.JSONDecodeError, TypeError):
            return []
    if not isinstance(categories, list):
        return []
    return [c for c in categories if c in CATEGORY_DESCRIPTIONS]


def extract_keywords_safe(text_or_json) -> list:
    """hobbies_and_interests_list (JSON 리스트) 또는 hobbies_and_interests (텍스트)에서 키워드 추출."""
    if pd.isna(text_or_json):
        return []
    try:
        keywords = json.loads(str(text_or_json))
        if isinstance(keywords, list):
            return [str(k).lower() for k in keywords if k]
    except (json.JSONDecodeError, TypeError):
        pass
    return []


def compute_video_features(videos_raw: pd.DataFrame, snapshot_date: str) -> pd.DataFrame:
    """영상 원본 컬럼(categoryId/duration/viewCount/...)에서 모델 영상 피처를 계산한다.

    channel_subscriber_count/channel_view_count/channel_video_count는
    videos_raw에 channelSubscriberCount/channelViewCount/channelVideoCount
    컬럼이 있을 때만 채우고, 없으면 0으로 default 처리한다
    (docs/guides/data-warehouse.md video_feature cold-start 규칙과 동일).
    """
    datetime.strptime(snapshot_date, "%Y-%m-%d")  # SQL 보간 전 형식 검증
    con = duckdb.connect()
    con.register("videos_raw", videos_raw)
    channel_subscriber_expr = (
        "CAST(channelSubscriberCount AS BIGINT)"
        if "channelSubscriberCount" in videos_raw.columns
        else "NULL"
    )
    channel_view_expr = (
        "CAST(channelViewCount AS BIGINT)"
        if "channelViewCount" in videos_raw.columns
        else "NULL"
    )
    channel_video_expr = (
        "CAST(channelVideoCount AS BIGINT)"
        if "channelVideoCount" in videos_raw.columns
        else "NULL"
    )
    return con.execute(
        f"""
        SELECT
            video_id,
            CAST(categoryId AS VARCHAR) AS category_id,
            COALESCE(CAST(duration AS INTEGER), 300) AS duration_sec,
            CAST(viewCount AS BIGINT) AS view_count,
            ROUND(CAST(likeCount AS FLOAT) / NULLIF(CAST(viewCount AS FLOAT), 0), 4) AS like_ratio,
            ROUND(CAST(commentCount AS FLOAT) / NULLIF(CAST(viewCount AS FLOAT), 0), 4) AS comment_ratio,
            DATE_DIFF('day', CAST(publishedAt AS DATE), DATE '{snapshot_date}') AS days_since_upload,
            COALESCE({channel_subscriber_expr}, 0) AS channel_subscriber_count,
            COALESCE({channel_view_expr}, 0) AS channel_view_count,
            COALESCE({channel_video_expr}, 0) AS channel_video_count
        FROM videos_raw
        """
    ).df()


def compute_user_offline_features(personas_raw: pd.DataFrame) -> pd.DataFrame:
    """persona 원본(uuid/age/occupation)에서 오프라인 유저 피처를 계산한다.

    watch_time_band는 personas_raw에 watch_time_band 컬럼이 있을 때만
    docs/guides/data-warehouse.md의 user_static_feature 정규화 규칙(오전/오후/
    저녁/밤 표기를 morning/evening/night로 통일, 그 외는 unknown)을 적용하고,
    컬럼이 없으면 "unknown"으로 default 처리한다.
    """
    con = duckdb.connect()
    con.register("personas_raw", personas_raw)
    watch_time_band_expr = (
        """
        CASE
            WHEN LOWER(TRIM(watch_time_band)) IN ('morning', 'am', '오전', '아침') THEN 'morning'
            WHEN LOWER(TRIM(watch_time_band)) IN ('evening', 'pm', '저녁', '오후') THEN 'evening'
            WHEN LOWER(TRIM(watch_time_band)) IN ('night', 'late_night', '밤', '심야') THEN 'night'
            ELSE 'unknown'
        END
        """
        if "watch_time_band" in personas_raw.columns
        else "'unknown'"
    )
    return con.execute(
        f"""
        SELECT
            uuid AS user_id,
            CASE
                WHEN age < 20 THEN '10s'
                WHEN age < 30 THEN '20s'
                WHEN age < 40 THEN '30s'
                WHEN age < 50 THEN '40s'
                ELSE '50s+'
            END AS age_group,
            occupation,
            {watch_time_band_expr} AS watch_time_band
        FROM personas_raw
        """
    ).df()


def compute_point_in_time_user_features(
    event_log: pd.DataFrame,
    videos_raw: pd.DataFrame,
    query_points: pd.DataFrame,
) -> pd.DataFrame:
    """query_points(user_id, as_of[, carry...])의 각 행에 대해 as_of 기준
    historical_category_affinity와 recent 집계를 계산한다.

    스펙(docs/guides/data-warehouse.md의 user_dynamic_feature)은 **일 단위
    snapshot**이다: as_of의 날짜(d = CAST(as_of AS DATE))를 기준으로 recent_*_7d는
    `[d-7, d)`(당일 제외 7일) window, historical_category_affinity는 `[d-30, d)`
    30일 window 최빈 category다. 같은 (user_id, d)의 impression은 동일 피처를
    가지므로, event_log을 (user, day)로 미리 집계하고 (user, day) 그레인에서만
    계산한 뒤 query_points로 broadcast한다 — impression 단위 상관 서브쿼리의
    메모리 폭발(부등호 self-join 중간 결과)을 없앤다(#284, #271 근본 수정).

    스냅샷 당일 이벤트가 없어도(시뮬레이션 경로 등) 직전 window 활동이 반영되도록
    "이벤트 날 ∪ 스냅샷 날" spine 위에서 0-패딩 후 window를 돌린다. 학습 경로는
    query_points=노출 이벤트(as_of=impression 시각), 시뮬레이션 경로는
    query_points=유저×기준시각 1행으로 호출하며 같은 SQL이라 두 경로가 일치한다.

    recent_view_count_7d/total_event_count_7d는 근사값이다: event_log는
    long-format action_log가 아니라 impression 1행에 clicked/liked/
    watch_time_sec를 붙인 wide-format 어댑터(derive_wide_events() 참고)라
    view를 별도 행으로 셀 수 없다. watch_time_sec > 0을 view 발생으로,
    "impression 1 + clicked + view + liked" 합을 total_event_count_7d로
    근사한다. docs/guides/data-warehouse.md의 user_dynamic_feature(Feast
    경유 목표 설계)는 raw event_type을 직접 카운트하므로 이 근사와 다를 수
    있다 — Feast 전환(#207) 이후에는 이 근사가 필요 없어진다.
    """
    con = duckdb.connect()
    con.register("event_log_src", event_log)
    con.register("videos_raw", videos_raw)
    con.register("query_points_src", query_points)
    carry = [c for c in query_points.columns if c not in ("user_id", "as_of")]
    carry_select = "".join(f'q."{name}",\n            ' for name in carry)
    return con.execute(
        f"""
        WITH
        -- 스펙(data-warehouse.md user_dynamic_feature)은 일 단위 snapshot
        -- (Asia/Seoul 날짜 경계)이라, 피처는 (user_id, 날짜) 그레인에서만 다르다.
        -- 따라서 impression(수백만) 단위가 아니라 (user, day) 단위로만 계산하고
        -- 마지막에 query_points로 broadcast한다. 무거운 윈도우 연산이 작은
        -- (user, day) 테이블 위에서만 돌아 상관 서브쿼리의 메모리 폭발을 없앤다(#284).

        -- event_log은 wide-format(impression 1행 + clicked/liked/watch_time_sec)
        -- 이라 view를 별도 행으로 못 세고, watch_time_sec > 0을 view로 근사한다
        -- (docstring 참고). day 단위로 미리 합산한다.
        daily AS (
            SELECT
                user_id,
                CAST(timestamp AS DATE) AS event_date,
                SUM(CASE WHEN clicked = 1 THEN 1 ELSE 0 END) AS click_c,
                SUM(CASE WHEN CAST(watch_time_sec AS BIGINT) > 0 THEN 1 ELSE 0 END) AS view_c,
                COALESCE(SUM(watch_time_sec), 0) AS watch_s,
                SUM(CASE WHEN liked = 1 THEN 1 ELSE 0 END) AS like_c,
                COUNT(*)
                    + SUM(CASE WHEN clicked = 1 THEN 1 ELSE 0 END)
                    + SUM(CASE WHEN CAST(watch_time_sec AS BIGINT) > 0 THEN 1 ELSE 0 END)
                    + SUM(CASE WHEN liked = 1 THEN 1 ELSE 0 END) AS tot_c
            FROM event_log_src
            GROUP BY user_id, event_date
        ),
        -- 스냅샷 날짜(= as_of의 날짜). 같은 (user, day)의 여러 impression은
        -- 한 번만 계산한다.
        snap AS (
            SELECT DISTINCT user_id, CAST(as_of AS DATE) AS d
            FROM query_points_src
        ),
        -- spine: 이벤트 날 ∪ 스냅샷 날. 스냅샷 당일 이벤트가 0건이어도 그 날의
        -- rolling이 계산되도록 0-패딩한다 — 안 하면 window가 그 (user, day) 행을
        -- 아예 안 만들어, 직전 6일 활동이 있어도 조용히 0으로 떨어진다.
        spine AS (
            SELECT user_id, event_date AS d FROM daily
            UNION
            SELECT user_id, d FROM snap
        ),
        daily_padded AS (
            SELECT
                s.user_id,
                s.d AS event_date,
                COALESCE(dl.click_c, 0) AS click_c,
                COALESCE(dl.view_c, 0) AS view_c,
                COALESCE(dl.watch_s, 0) AS watch_s,
                COALESCE(dl.like_c, 0) AS like_c,
                COALESCE(dl.tot_c, 0) AS tot_c
            FROM spine s
            LEFT JOIN daily dl ON dl.user_id = s.user_id AND dl.event_date = s.d
        ),
        -- 7일 rolling: [d-7, d) = d-7 ~ d-1 (당일 제외, 스펙 window와 1:1).
        recent AS (
            SELECT
                user_id,
                event_date AS d,
                SUM(click_c) OVER w AS recent_click_count_7d,
                SUM(view_c) OVER w AS recent_view_count_7d,
                SUM(watch_s) OVER w AS recent_watch_time_7d,
                SUM(like_c) OVER w AS recent_like_count_7d,
                SUM(tot_c) OVER w AS total_event_count_7d
            FROM daily_padded
            WINDOW w AS (
                PARTITION BY user_id ORDER BY event_date
                RANGE BETWEEN INTERVAL 7 DAY PRECEDING AND INTERVAL 1 DAY PRECEDING
            )
        ),
        -- historical_category_affinity: (user, day, category)별 반응(클릭·시청·
        -- 좋아요) 일별 카운트. video_category는 videos JOIN으로 한 번만 붙인다.
        cat_daily AS (
            SELECT
                e.user_id,
                CAST(e.timestamp AS DATE) AS event_date,
                CAST(v.categoryId AS VARCHAR) AS category_id,
                SUM(
                    CASE WHEN e.clicked = 1
                          OR CAST(e.watch_time_sec AS BIGINT) > 0
                          OR e.liked = 1
                         THEN 1 ELSE 0 END
                ) AS react_c
            FROM event_log_src e
            JOIN videos_raw v ON v.video_id = e.video_id
            GROUP BY e.user_id, event_date, category_id
        ),
        -- 스냅샷 기준 30일 [d-30, d) 윈도우에서 반응 최다 category를 고른다.
        -- snap이 구동하므로 스냅샷 당일 이벤트가 없어도(30일 내 이력만 있으면)
        -- 정상 계산되고, 30일 내 이력이 전혀 없으면 매칭이 없어 'unknown'이 된다.
        affinity_ranked AS (
            SELECT
                s.user_id,
                s.d,
                c.category_id,
                ROW_NUMBER() OVER (
                    PARTITION BY s.user_id, s.d
                    ORDER BY SUM(c.react_c) DESC, c.category_id
                ) AS rn
            FROM snap s
            JOIN cat_daily c
              ON c.user_id = s.user_id
             AND c.event_date >= s.d - INTERVAL 30 DAY
             AND c.event_date < s.d
             AND c.react_c > 0
            GROUP BY s.user_id, s.d, c.category_id
        ),
        affinity AS (
            SELECT user_id, d, category_id AS historical_category_affinity
            FROM affinity_ranked WHERE rn = 1
        )
        SELECT
            q.user_id,
            q.as_of,
            {carry_select}COALESCE(a.historical_category_affinity, 'unknown') AS historical_category_affinity,
            COALESCE(r.recent_click_count_7d, 0) AS recent_click_count_7d,
            COALESCE(r.recent_watch_time_7d, 0) AS recent_watch_time_7d,
            COALESCE(r.recent_like_count_7d, 0) AS recent_like_count_7d,
            COALESCE(r.recent_view_count_7d, 0) AS recent_view_count_7d,
            COALESCE(r.total_event_count_7d, 0) AS total_event_count_7d
        FROM query_points_src q
        LEFT JOIN recent r
            ON r.user_id = q.user_id AND r.d = CAST(q.as_of AS DATE)
        LEFT JOIN affinity a
            ON a.user_id = q.user_id AND a.d = CAST(q.as_of AS DATE)
        """
    ).df()


def compute_user_topic_features(
    personas_raw: pd.DataFrame, category_ids, skip_embedding: bool = False
) -> pd.DataFrame:
    """유저 x category_id 단위로 topic_similarity/preferred_category_match를 선계산한다.

    hobbies_and_interests_list/primary_categories는 persona당 1개뿐인데, 이벤트
    (impression) 단위로 조인하면 유저당 평균 노출 수만큼 리스트/임베딩 컬럼이
    복제되어 대규모 데이터에서 OOM을 유발한다(#231/#238 이후에도 재현, #240).
    topic_similarity/preferred_category_match는 실제로는 (user, category_id)
    조합에만 의존하고 category_id는 관측되는 값이 적으므로, persona 수 x
    distinct category 수(이벤트 수보다 훨씬 작음) 크기로 미리 계산해두면
    이벤트 조인 시에는 float/int 컬럼만 붙이면 된다.

    Args:
        personas_raw: uuid/hobbies_and_interests_list[/primary_categories] 컬럼을 가진 persona 원본.
        category_ids: 이벤트 쪽에 실제로 존재하는 category_id 값들
            (예: video_feature["category_id"].unique()).
        skip_embedding: True면 embed_texts() 호출(Vertex AI)을 건너뛰고
            topic_similarity를 전부 None으로 채운다. 호출자가 topic_similarity를
            사전 계산된 BigQuery `user_category_similarity`에서 별도로 가져오고
            이 함수는 preferred_category_match만 쓸 때 사용한다(#214) —
            preferred_category_match 산출 로직을 별도 함수로 복제하면 나중에
            매칭 규칙이 바뀔 때 한쪽만 고치는 drift가 생기므로, 같은 함수를
            공유한다.

    Returns:
        user_id, category_id, topic_similarity, preferred_category_match 컬럼을
        가진 DataFrame (행 수 = len(personas_raw) x len(distinct category_ids)).
    """
    preferred_topics = personas_raw["hobbies_and_interests_list"].apply(extract_keywords_safe)
    if "primary_categories" in personas_raw.columns:
        preferred_category = personas_raw["primary_categories"].apply(parse_primary_categories)
    else:
        preferred_category = preferred_topics.apply(derive_preferred_category)

    if skip_embedding:
        user_keyword_embeddings = pd.Series([None] * len(personas_raw), index=personas_raw.index)
    else:
        unique_keywords = sorted({kw for kws in preferred_topics for kw in kws})
        keyword_vectors = embed_texts(unique_keywords, task_type="RETRIEVAL_QUERY")
        keyword_embedding_cache = dict(zip(unique_keywords, keyword_vectors))
        user_keyword_embeddings = preferred_topics.apply(
            lambda kws: [keyword_embedding_cache[kw] for kw in kws]
        )

    distinct_category_ids = list(dict.fromkeys(category_ids))
    rows = [
        {
            "user_id": user_id,
            "category_id": category_id,
            "topic_similarity": (
                None if skip_embedding else compute_topic_similarity(kw_embeddings, category_id)
            ),
            "preferred_category_match": compute_preferred_category_match(pref_cat, category_id),
        }
        for user_id, kw_embeddings, pref_cat in zip(
            personas_raw["uuid"], user_keyword_embeddings, preferred_category
        )
        for category_id in distinct_category_ids
    ]
    return pd.DataFrame(
        rows, columns=["user_id", "category_id", "topic_similarity", "preferred_category_match"]
    )


def compute_interaction_columns(joined: pd.DataFrame) -> pd.DataFrame:
    """preferred/topic/match 상호작용 피처를 계산해 컬럼으로 추가한다.

    입력 필수 컬럼: hobbies_and_interests_list, historical_category_affinity,
    category_id. (build_training_dataset.py Step 2의 계산을 그대로 이동한 것.)

    preferred_category는 joined에 primary_categories 컬럼이 있으면(virtual_users
    파이프라인의 실제 LLM 산출값, #205) 그 값을 그대로 쓰고, 없으면(구식 mock
    personas.csv 등) derive_preferred_category() 키워드 매핑 fallback을 쓴다.

    user_keyword_embeddings는 joined의 고유(unique) 키워드만 한 번씩 배치
    임베딩한다(#206) — joined는 유저 1명당 여러 행(impression마다 1행)을
    가지므로, 행마다 개별 임베딩하면 같은 키워드를 Vertex AI에 반복
    요청하게 된다.
    """
    out = joined.copy()
    out["preferred_topics"] = out["hobbies_and_interests_list"].apply(extract_keywords_safe)
    if "primary_categories" in out.columns:
        out["preferred_category"] = out["primary_categories"].apply(parse_primary_categories)
    else:
        out["preferred_category"] = out["preferred_topics"].apply(derive_preferred_category)

    unique_keywords = sorted({kw for kws in out["preferred_topics"] for kw in kws})
    keyword_vectors = embed_texts(unique_keywords, task_type="RETRIEVAL_QUERY")
    keyword_embedding_cache = dict(zip(unique_keywords, keyword_vectors))
    out["user_keyword_embeddings"] = out["preferred_topics"].apply(
        lambda kws: [keyword_embedding_cache[kw] for kw in kws]
    )
    out["topic_similarity"] = out.apply(
        lambda row: compute_topic_similarity(row["user_keyword_embeddings"], row["category_id"]),
        axis=1,
    )
    out["historical_category_match"] = out.apply(
        lambda row: compute_historical_category_match(
            row["historical_category_affinity"], row["category_id"]
        ),
        axis=1,
    )
    out["preferred_category_match"] = out.apply(
        lambda row: compute_preferred_category_match(row["preferred_category"], row["category_id"]),
        axis=1,
    )
    return out
