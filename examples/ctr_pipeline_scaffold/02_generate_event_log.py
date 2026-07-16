"""
02_generate_event_log.py

- docs/guides/agent-simulator-spec.md의 Phase 1(historical) 생성 규칙을 단순화하여 재현한다.
- docs/guides/ctr-model-specification.md의 Topic Vocabulary 기반 topic 추출 규칙을 따른다.

⚠️ 주의 (Placeholder 명시)
- 실제 스펙에서는 관심사/주제 추출을 "LLM 또는 Keyword 기반"으로 하고 구현 방식은 구현자 재량이다.
  여기서는 데모 목적으로 "Topic Vocabulary 키워드가 텍스트에 등장하는지" 여부로 단순 판정한다.
  (실제 구현 시 LLM 기반 추출로 교체 가능)
- 클릭 확률은 실제 학습 피처인 topic_similarity(src.features.feature_builder,
  docs/guides/ctr-model-specification.md 기준 user_keyword_embeddings ↔ category_description_embedding
  cosine 유사도)를 그대로 재사용해 계산한다. 별도의 근사치(예: Jaccard)를 쓰면 라벨과
  학습 피처의 신호가 어긋나 mock 데이터에서 모델이 아무것도 학습하지 못하게 된다.
  Agent Simulator 담당자가 실제 구현 시 다른 알고리즘을 쓸 수 있으나, 그 경우에도
  라벨 생성 로직과 실제 feature 계산 로직이 같은 신호를 가리키도록 맞춰야 한다.
"""

import os
import sys
import json
import random
from datetime import datetime, timedelta

import pandas as pd

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
sys.path.insert(0, PROJECT_ROOT)

from src.features.feature_builder import embed_keywords, compute_topic_similarity  # noqa: E402

random.seed(42)

TOPIC_VOCAB = [
    "music", "sports", "gaming", "travel", "food",
    "education", "technology", "beauty", "news",
    "entertainment", "family", "finance",
    "health", "movie", "fashion",
]


def extract_topics_from_text(text: str, k_max=4):
    """Topic Vocabulary 키워드 등장 여부 기반 단순 추출 (LLM 추출의 placeholder)."""
    text_lower = text.lower()
    found = [t for t in TOPIC_VOCAB if t in text_lower]
    return found[:k_max] if found else [random.choice(TOPIC_VOCAB)]


def main():
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")

    videos = pd.read_csv(os.path.join(data_dir, "video_raw.csv"))
    personas = pd.read_csv(os.path.join(data_dir, "persona_raw.csv"))

    # --- Topic Vocabulary 기반 추출 (preferred_topics / video_topic) ---
    videos["video_topic"] = videos.apply(
        lambda r: extract_topics_from_text(
            f"{r['title']} {r['description']} {r['tags']}"
        ),
        axis=1,
    )
    personas["preferred_topics"] = personas.apply(
        lambda r: extract_topics_from_text(
            f"{r['persona']} {r['hobbies_and_interests']} {r['professional_persona']} "
            f"{r['sports_persona']} {r['arts_persona']} {r['travel_persona']} "
            f"{r['culinary_persona']} {r['family_persona']}"
        ),
        axis=1,
    )

    videos[["video_id", "video_topic", "categoryId"]].assign(
        video_topic=lambda d: d["video_topic"].apply(json.dumps)
    ).to_csv(os.path.join(data_dir, "video_topic.csv"), index=False)

    personas[["uuid", "preferred_topics"]].assign(
        preferred_topics=lambda d: d["preferred_topics"].apply(json.dumps)
    ).to_csv(os.path.join(data_dir, "user_preferred_topics.csv"), index=False)

    # --- Phase 1 Event Log 생성 ---
    # agent-simulator-spec.md: "유저 관심사와 영상 주제가 가까울수록 clicked=1 확률이 높아지도록 생성"
    # "전체 row 중 clicked=1 비율은 약 2% 내외를 목표로 한다"
    N_IMPRESSIONS = 30000  # 노출(row) 수 = impression 수 (스펙: row 존재 = impression)
    START = datetime(2026, 4, 1)
    END = datetime(2026, 6, 30)  # 약 90일 범위 (Event Log Spec: 현실적 분산)
    span_sec = int((END - START).total_seconds())

    users = personas.to_dict("records")
    vids = videos.to_dict("records")

    video_category_map = dict(zip(videos["video_id"], videos["categoryId"]))

    # 클릭 라벨은 실제 학습 피처(topic_similarity, src.features.feature_builder)와 동일한
    # 로직으로 생성한다: hobbies_and_interests_list -> user_keyword_embeddings -> category_id
    # 와의 cosine 유사도(max-pool). build_training_dataset.py의 topic_similarity 계산과
    # 신호가 어긋나면(예: 별개의 Jaccard 근사치를 쓰면) mock 데이터에서 모델이 학습할
    # 신호 자체가 사라진다.
    user_keyword_embeddings_map = {
        row["uuid"]: embed_keywords(json.loads(row["hobbies_and_interests_list"]))
        for row in personas.to_dict("records")
    }

    rows = []
    # 유저별 하루 이벤트 수가 한쪽에 몰리지 않도록, 유저를 반복 샘플링하되 균등에 가깝게 배분
    user_cycle = list(range(len(users))) * (N_IMPRESSIONS // len(users) + 1)
    random.shuffle(user_cycle)

    for i in range(N_IMPRESSIONS):
        u = users[user_cycle[i] % len(users)]
        v = random.choice(vids)

        match_score = compute_topic_similarity(
            user_keyword_embeddings_map[u["uuid"]], video_category_map[v["video_id"]]
        )  # 0.0 ~ 1.0

        # base_rate를 아주 낮게 설정하고 match_score에 비례해 가산 -> 전체 평균 ~2%
        base_rate = -0.02
        boost = 0.30 * match_score
        click_prob = min(max(base_rate + boost, 0.0), 0.35)
        clicked = 1 if random.random() < click_prob else 0

        ts = START + timedelta(seconds=random.randint(0, span_sec))

        if clicked:
            watch_time_sec = int(random.uniform(0.2, 1.0) * random.randint(60, 1200))
            liked = 1 if random.random() < 0.3 else 0
        else:
            watch_time_sec = 0
            liked = 0

        rows.append(
            {
                "event_id": f"e{i:06d}",
                "timestamp": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
                "user_id": u["uuid"],
                "video_id": v["video_id"],
                "clicked": clicked,
                "watch_time_sec": watch_time_sec,
                "liked": liked,
                "search_keyword": None,
                "source": "historical",  # Phase 1
                "rank": None,  # Phase 1은 null
                "exposure_type": None,  # Phase 1은 null
            }
        )

    event_log = pd.DataFrame(rows).sort_values("timestamp").reset_index(drop=True)
    event_log.to_csv(os.path.join(data_dir, "event_log.csv"), index=False)

    click_ratio = event_log["clicked"].mean()
    print(f"event_log.csv: {len(event_log)} rows")
    print(f"clicked=1 비율: {click_ratio:.3%}  (목표: 약 2% 내외)")
    print(event_log["clicked"].value_counts())


if __name__ == "__main__":
    main()
