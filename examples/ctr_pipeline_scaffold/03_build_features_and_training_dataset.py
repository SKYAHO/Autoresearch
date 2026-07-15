"""
03_build_features_and_training_dataset.py

docs/guides/ctr-model-specification.md를 그대로 구현한다.
멘토 피드백("피처 가공은 pandas 연산보다 SQL 기반으로")에 따라
모든 Feature 가공은 DuckDB SQL로 수행한다. (Python은 orchestration만 담당)

산출물:
- data/video_feature.csv          (Video Feature — Baseline)
- data/user_feature_offline.csv   (User Static Feature — Baseline)
- data/training_dataset.csv       (최종 Model Input Columns, spec과 동일 순서)

⚠️ Placeholder 명시
- user_video_embedding_similarity: 실제 Sentence Transformer 대신,
  텍스트 해시 기반 결정론적 pseudo-embedding으로 cosine similarity를 계산한다.
  (이 환경은 HuggingFace 모델 다운로드가 불가하여 실제 임베딩 모델을 쓸 수 없음 — 반드시 실제 구현 시 교체 필요)
"""

import os
import hashlib
import json

import duckdb
import numpy as np
import pandas as pd

DATA_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
SNAPSHOT_DATE = "2026-07-02"  # days_since_upload 계산 기준일


# ---------------------------------------------------------------------------
# Placeholder: pseudo-embedding (실제로는 Sentence Transformer로 교체)
# ---------------------------------------------------------------------------
def pseudo_embedding(text: str, dim: int = 32) -> np.ndarray:
    h = hashlib.sha256(text.encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
    v = rng.normal(size=dim)
    return v / np.linalg.norm(v)


def cosine(a: np.ndarray, b: np.ndarray) -> float:
    return float(np.dot(a, b))


def main():
    con = duckdb.connect()

    videos = pd.read_csv(os.path.join(DATA_DIR, "video_raw.csv"))
    video_topic = pd.read_csv(os.path.join(DATA_DIR, "video_topic.csv"))
    personas = pd.read_csv(os.path.join(DATA_DIR, "persona_raw.csv"))
    user_topics = pd.read_csv(os.path.join(DATA_DIR, "user_preferred_topics.csv"))
    event_log = pd.read_csv(os.path.join(DATA_DIR, "event_log.csv"))

    con.register("videos_raw", videos)
    con.register("video_topic", video_topic)
    con.register("personas_raw", personas)
    con.register("user_topics", user_topics)
    con.register("event_log", event_log)

    # =========================================================
    # 1) Video Feature (Baseline) — SQL
    # =========================================================
    video_feature = con.execute(
        f"""
        SELECT
            video_id,
            CAST(categoryId AS VARCHAR)                        AS category_id,
            -- duration은 raw에서 "PT4M29S" 형식이라 파이썬 단계에서 미리 초로 변환해도 되지만
            -- 여기선 SQL 정규식으로 변환 (분/초 각각 추출)
            CAST(regexp_extract(duration, 'PT(\\d+)M', 1) AS INTEGER) * 60
              + CAST(regexp_extract(duration, 'M(\\d+)S', 1) AS INTEGER)  AS duration_sec,
            viewCount                                          AS view_count,
            ROUND(likeCount * 1.0 / NULLIF(viewCount, 0), 4)   AS like_ratio,
            ROUND(commentCount * 1.0 / NULLIF(viewCount, 0), 4) AS comment_ratio,
            DATE_DIFF('day', CAST(publishedAt AS DATE), DATE '{SNAPSHOT_DATE}') AS days_since_upload
        FROM videos_raw
        """
    ).df()
    video_feature.to_csv(os.path.join(DATA_DIR, "video_feature.csv"), index=False)

    # =========================================================
    # 2) User Feature — Offline (Baseline, Static) — SQL
    # =========================================================
    user_feature_offline = con.execute(
        """
        SELECT
            uuid AS user_id,
            CASE
                WHEN age < 20 THEN '10s'
                WHEN age < 30 THEN '20s'
                WHEN age < 40 THEN '30s'
                WHEN age < 50 THEN '40s'
                ELSE '50s+'
            END AS age_group,
            occupation
        FROM personas_raw
        """
    ).df()
    user_feature_offline.to_csv(os.path.join(DATA_DIR, "user_feature_offline.csv"), index=False)

    # =========================================================
    # 3) User Feature — Online (Baseline, Point-in-time) — SQL
    #    Label Timestamp 이전 이벤트만 집계 (Point-in-time Correctness)
    #    실제 구현에서는 Online Feature Store(Redis)를 조회하지만,
    #    여기서는 원리를 검증하기 위해 correlated subquery로 재현한다.
    # =========================================================
    con.execute(
        """
        CREATE OR REPLACE TABLE base_events AS
        SELECT
            event_id, CAST(timestamp AS TIMESTAMP) AS timestamp, user_id, video_id, clicked
        FROM event_log
        """
    )
    con.execute(
        """
        CREATE OR REPLACE TABLE event_log_ts AS
        SELECT
            event_id, CAST(timestamp AS TIMESTAMP) AS timestamp, user_id, video_id,
            clicked, watch_time_sec, liked, search_keyword, source, rank, exposure_type
        FROM event_log
        """
    )

    online_features = con.execute(
        """
        SELECT
            e.event_id,
            e.user_id,
            e.video_id,
            e.timestamp,
            e.clicked,

            -- historical_category_affinity: label timestamp 이전, 클릭한 영상들 중 최빈 category
            -- Cold-start Policy: 과거 클릭 이력이 없으면 'unknown'으로 채운다
            COALESCE(
                (
                    SELECT CAST(v.categoryId AS VARCHAR)
                    FROM event_log_ts AS past
                    JOIN videos_raw AS v ON v.video_id = past.video_id
                    WHERE past.user_id = e.user_id
                      AND past.timestamp < e.timestamp
                      AND past.clicked = 1
                    GROUP BY v.categoryId
                    ORDER BY COUNT(*) DESC
                    LIMIT 1
                ),
                'unknown'
            ) AS historical_category_affinity,

            -- recent_click_count_7d
            (
                SELECT COUNT(*)
                FROM event_log_ts AS past
                WHERE past.user_id = e.user_id
                  AND past.clicked = 1
                  AND past.timestamp < e.timestamp
                  AND past.timestamp >= e.timestamp - INTERVAL 7 DAY
            ) AS recent_click_count_7d,

            -- recent_watch_time_7d
            (
                SELECT COALESCE(SUM(past.watch_time_sec), 0)
                FROM event_log_ts AS past
                WHERE past.user_id = e.user_id
                  AND past.timestamp < e.timestamp
                  AND past.timestamp >= e.timestamp - INTERVAL 7 DAY
            ) AS recent_watch_time_7d,

            -- recent_like_count_7d
            (
                SELECT COUNT(*)
                FROM event_log_ts AS past
                WHERE past.user_id = e.user_id
                  AND past.liked = 1
                  AND past.timestamp < e.timestamp
                  AND past.timestamp >= e.timestamp - INTERVAL 7 DAY
            ) AS recent_like_count_7d

        FROM base_events e
        """
    ).df()

    # =========================================================
    # 4) Interaction Feature — category_match, topic_similarity (SQL)
    #    user_video_embedding_similarity는 placeholder embedding으로 Python에서 계산 후 join
    # =========================================================
    con.register("online_features", online_features)

    joined = con.execute(
        """
        SELECT
            o.event_id, o.user_id, o.video_id, o.timestamp, o.clicked,
            o.historical_category_affinity, o.recent_click_count_7d,
            o.recent_watch_time_7d, o.recent_like_count_7d,
            vf.category_id, vf.duration_sec, vf.view_count, vf.like_ratio,
            vf.comment_ratio, vf.days_since_upload,
            CASE
                WHEN o.historical_category_affinity = 'unknown' THEN 0
                WHEN o.historical_category_affinity = vf.category_id THEN 1
                ELSE 0
            END AS category_match,
            ut.preferred_topics,
            vt.video_topic
        FROM online_features o
        JOIN video_feature vf ON vf.video_id = o.video_id
        JOIN user_topics ut ON ut.uuid = o.user_id
        JOIN video_topic vt ON vt.video_id = o.video_id
        """
    ).df()

    def jaccard_str(a, b):
        sa, sb = set(json.loads(a)), set(json.loads(b))
        if not sa or not sb:
            return 0.0
        return len(sa & sb) / len(sa | sb)

    joined["topic_similarity"] = joined.apply(
        lambda r: jaccard_str(r["preferred_topics"], r["video_topic"]), axis=1
    )

    # user_video_embedding_similarity — placeholder pseudo-embedding
    user_text_map = {
        row["uuid"]: pseudo_embedding(str(row["persona"]) + str(row["hobbies_and_interests"]))
        for _, row in personas.iterrows()
    }
    video_text_map = {
        row["video_id"]: pseudo_embedding(str(row["title"]) + str(row["description"]))
        for _, row in videos.iterrows()
    }
    joined["user_video_embedding_similarity"] = joined.apply(
        lambda r: round(cosine(user_text_map[r["user_id"]], video_text_map[r["video_id"]]), 4),
        axis=1,
    )

    # =========================================================
    # 5) User Static Feature(age_group, occupation) 붙이고
    #    최종 Model Input Columns 순서로 정렬 (docs/guides/ctr-model-specification.md 기준)
    # =========================================================
    con.register("joined", joined)
    con.register("user_feature_offline", user_feature_offline)

    training_dataset = con.execute(
        """
        SELECT
            uo.age_group,
            uo.occupation,
            j.historical_category_affinity,
            j.recent_click_count_7d,
            j.recent_watch_time_7d,
            j.recent_like_count_7d,
            j.category_id,
            j.duration_sec,
            j.view_count,
            j.like_ratio,
            j.comment_ratio,
            j.days_since_upload,
            j.category_match,
            j.topic_similarity,
            j.user_video_embedding_similarity,
            j.clicked
        FROM joined j
        JOIN user_feature_offline uo ON uo.user_id = j.user_id
        ORDER BY j.timestamp
        """
    ).df()

    training_dataset.to_csv(os.path.join(DATA_DIR, "training_dataset.csv"), index=False)

    print(f"video_feature.csv: {len(video_feature)} rows")
    print(f"user_feature_offline.csv: {len(user_feature_offline)} rows")
    print(f"training_dataset.csv: {len(training_dataset)} rows, {training_dataset.shape[1]} columns")
    print("\nclicked ratio:", training_dataset["clicked"].mean())
    print("\ncolumns:", list(training_dataset.columns))
    print("\nhead:")
    print(training_dataset.head(5).to_string())
    print("\nnull check (historical_category_affinity is coalesced to 'unknown' for cold-start):")
    print(training_dataset.isnull().sum())


if __name__ == "__main__":
    main()
