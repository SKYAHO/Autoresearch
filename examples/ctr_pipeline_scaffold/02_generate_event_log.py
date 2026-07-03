"""
02_generate_event_log.py

- AGENT_SIMULATOR_SPEC.md의 Phase 1(historical) 생성 규칙을 단순화하여 재현한다.
- CTR_Model_Specification.md의 Topic Vocabulary 기반 topic 추출 규칙을 따른다.

⚠️ 주의 (Placeholder 명시)
- 실제 스펙에서는 관심사/주제 추출을 "LLM 또는 Keyword 기반"으로 하고 구현 방식은 구현자 재량이다.
  여기서는 데모 목적으로 "Topic Vocabulary 키워드가 텍스트에 등장하는지" 여부로 단순 판정한다.
  (실제 구현 시 LLM 기반 추출로 교체 가능)
- 매칭 알고리즘(Persona 관심사 ↔ 영상 주제)도 구현자 재량이며, 여기서는
  Jaccard 유사도 + category 일치 가중치로 단순화했다. Agent Simulator 담당자가
  실제 구현 시 다른 알고리즘을 쓸 수 있다 — 이 스크립트는 어디까지나
  "Event Log 스키마와 CTR Model Spec이 실제로 맞물려 동작하는지"를 검증하기 위한 것이다.
"""

import os
import json
import random
from datetime import datetime, timedelta

import pandas as pd

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


def jaccard(a: list, b: list) -> float:
    sa, sb = set(a), set(b)
    if not sa or not sb:
        return 0.0
    return len(sa & sb) / len(sa | sb)


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
    # AGENT_SIMULATOR_SPEC: "유저 관심사와 영상 주제가 가까울수록 clicked=1 확률이 높아지도록 생성"
    # "전체 row 중 clicked=1 비율은 약 2% 내외를 목표로 한다"
    N_IMPRESSIONS = 6000  # 노출(row) 수 = impression 수 (스펙: row 존재 = impression)
    START = datetime(2026, 4, 1)
    END = datetime(2026, 6, 30)  # 약 90일 범위 (Event Log Spec: 현실적 분산)
    span_sec = int((END - START).total_seconds())

    users = personas.to_dict("records")
    vids = videos.to_dict("records")

    user_topics_map = dict(zip(personas["uuid"], personas["preferred_topics"]))
    video_topics_map = dict(zip(videos["video_id"], videos["video_topic"]))

    rows = []
    # 유저별 하루 이벤트 수가 한쪽에 몰리지 않도록, 유저를 반복 샘플링하되 균등에 가깝게 배분
    user_cycle = list(range(len(users))) * (N_IMPRESSIONS // len(users) + 1)
    random.shuffle(user_cycle)

    for i in range(N_IMPRESSIONS):
        u = users[user_cycle[i] % len(users)]
        v = random.choice(vids)

        u_topics = user_topics_map[u["uuid"]]
        v_topics = video_topics_map[v["video_id"]]

        sim = jaccard(u_topics, v_topics)
        # category 일치 가중치는 여기선 topic 기반 근사치로 대체 (raw category 텍스트 매핑 단순화)
        match_score = sim  # 0.0 ~ 1.0

        # base_rate를 아주 낮게 설정하고 match_score에 비례해 소폭 가산 -> 전체 평균 ~2%
        base_rate = 0.014
        boost = 0.12 * match_score
        click_prob = min(base_rate + boost, 0.35)
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
