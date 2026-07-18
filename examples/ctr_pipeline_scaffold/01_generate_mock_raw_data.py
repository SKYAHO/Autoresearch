"""
01_generate_mock_raw_data.py

docs/guides/ctr-model-specification.md의 Raw Data 스키마(YouTube Data API + Persona)를 그대로 따르는
Mock Raw 데이터를 생성한다.

이 스크립트가 만드는 데이터는:
- YouTube API 실제 호출 X (스키마만 동일하게 재현)
- NVIDIA Persona 데이터셋 실제 다운로드 X (스키마만 동일하게 재현)
=> 파이프라인/스펙 검증용 예시 데이터이며, 실제 데이터 수집 파이프라인(영준 님 담당)을 대체하지 않는다.
"""

import os
import sys
import random
import json
import pandas as pd
from datetime import datetime, timedelta

# Add PROJECT_ROOT to sys.path for imports
PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__)))))
sys.path.insert(0, PROJECT_ROOT)

from src.features.category_reference import CATEGORY_DESCRIPTIONS

random.seed(42)

TOPIC_VOCAB = [
    "music", "sports", "gaming", "travel", "food",
    "education", "technology", "beauty", "news",
    "entertainment", "family", "finance",
    "health", "movie", "fashion",
]

# Topic -> YouTube category name 매핑 (mock)
TOPIC_TO_CATEGORY = {
    "music": "Music", "sports": "Sports", "gaming": "Gaming", "travel": "Travel & Events",
    "food": "Howto & Style", "education": "Education", "technology": "Science & Technology", "beauty": "Howto & Style",
    "news": "News & Politics", "entertainment": "Entertainment", "family": "Film & Animation", "finance": "News & Politics",
    "health": "Howto & Style", "movie": "Film & Animation", "fashion": "Howto & Style",
}

assert set(TOPIC_TO_CATEGORY.values()) <= set(CATEGORY_DESCRIPTIONS), \
    f"TOPIC_TO_CATEGORY has invalid categories: {set(TOPIC_TO_CATEGORY.values()) - set(CATEGORY_DESCRIPTIONS)}"

CHANNELS = [f"Channel_{t.capitalize()}{i}" for t in TOPIC_VOCAB for i in range(1, 3)]

VIDEO_TEMPLATES = {
    "music": ["신곡 라이브 무대", "K-POP 커버 챌린지", "플레이리스트 모음"],
    "sports": ["축구 하이라이트", "농구 경기 분석", "홈트레이닝 루틴"],
    "gaming": ["신작 게임 플레이 영상", "FPS 랭크 하이라이트", "게임 공략 가이드"],
    "travel": ["국내 여행 브이로그", "해외 배낭여행 기록", "숨은 여행지 추천"],
    "food": ["집밥 레시피", "맛집 탐방 브이로그", "베이킹 튜토리얼"],
    "education": ["프로그래밍 강의", "영어 회화 강좌", "자격증 공부법"],
    "technology": ["신제품 리뷰", "IT 뉴스 정리", "개발자 인터뷰"],
    "beauty": ["메이크업 튜토리얼", "스킨케어 루틴", "제품 리뷰"],
    "news": ["오늘의 뉴스 브리핑", "시사 이슈 정리", "경제 뉴스 요약"],
    "entertainment": ["예능 하이라이트", "웹드라마 클립", "밈 모음"],
    "family": ["육아 브이로그", "가족 여행 기록", "홈스쿨링 팁"],
    "finance": ["재테크 기초 강의", "주식 시장 분석", "가계부 관리법"],
    "health": ["다이어트 루틴", "명상 가이드", "건강식단 소개"],
    "movie": ["영화 리뷰", "예고편 반응", "명장면 모음"],
    "fashion": ["코디 추천", "쇼핑 하울", "브랜드 소개"],
}


def gen_videos(n=30):
    rows = []
    for i in range(n):
        topic = TOPIC_VOCAB[i % len(TOPIC_VOCAB)]
        title = f"{random.choice(VIDEO_TEMPLATES[topic])} #{i}"
        description = f"이 영상은 {topic} 주제를 다룹니다. {title}에 대한 상세 설명입니다."
        tags = [topic, random.choice(TOPIC_VOCAB)]
        published_days_ago = random.randint(1, 180)
        published_at = (datetime(2026, 7, 2) - timedelta(days=published_days_ago)).strftime(
            "%Y-%m-%dT%H:%M:%SZ"
        )
        view_count = random.randint(500, 2_000_000)
        like_count = int(view_count * random.uniform(0.01, 0.08))
        comment_count = int(view_count * random.uniform(0.001, 0.01))
        duration_sec = random.randint(60, 1500)
        rows.append(
            {
                "video_id": f"v{i:04d}",
                "title": title,
                "description": description,
                "channelTitle": random.choice(CHANNELS),
                "publishedAt": published_at,
                "categoryId": TOPIC_TO_CATEGORY[topic],
                "tags": json.dumps(tags, ensure_ascii=False),
                "viewCount": view_count,
                "likeCount": like_count,
                "commentCount": comment_count,
                "duration": f"PT{duration_sec // 60}M{duration_sec % 60}S",
                "language": "ko",
                # 검증 편의를 위한 ground-truth 컬럼 (실제 raw data에는 없음, QA용)
                "_true_topic": topic,
            }
        )
    return pd.DataFrame(rows)


OCCUPATIONS = ["Engineer", "Designer", "Teacher", "Marketer", "Student", "Nurse", "Chef", "Analyst"]
DISTRICTS = ["강남구", "마포구", "해운대구", "수영구", "유성구"]
PROVINCES = ["서울", "부산", "대전"]


def gen_personas(n=50):
    rows = []
    for i in range(n):
        k = random.randint(2, 3)
        interests = random.sample(TOPIC_VOCAB, k=k)
        hobby_text = ", ".join(interests)
        rows.append(
            {
                "uuid": f"u{i:04d}",
                "professional_persona": f"{random.choice(OCCUPATIONS)}로 일하며 {interests[0]} 관련 콘텐츠를 즐겨봄",
                "sports_persona": "스포츠를 즐겨 시청함" if "sports" in interests else "스포츠에 큰 관심 없음",
                "arts_persona": "예술/영화 콘텐츠를 좋아함" if "movie" in interests else "예술 분야 관심 적음",
                "travel_persona": "여행 브이로그를 자주 봄" if "travel" in interests else "여행 콘텐츠 관심 적음",
                "culinary_persona": "요리/맛집 콘텐츠를 즐김" if "food" in interests else "음식 콘텐츠 관심 적음",
                "family_persona": "가족 중심 콘텐츠 선호" if "family" in interests else "가족 콘텐츠 관심 적음",
                "persona": f"{hobby_text}에 관심이 많은 시청자",
                "hobbies_and_interests": f"주요 관심사: {hobby_text}",
                "hobbies_and_interests_list": json.dumps(interests, ensure_ascii=False),
                "sex": random.choice(["M", "F"]),
                "age": random.randint(18, 60),
                "occupation": random.choice(OCCUPATIONS),
                "district": random.choice(DISTRICTS),
                "province": random.choice(PROVINCES),
                "country": "KR",
                # 검증 편의를 위한 ground-truth 컬럼 (실제 raw data에는 없음, QA용)
                "_true_interests": json.dumps(interests, ensure_ascii=False),
            }
        )
    return pd.DataFrame(rows)


def main():
    """Generate mock raw data (videos and personas)."""
    data_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
    videos = gen_videos(30)
    personas = gen_personas(50)
    videos.to_csv(os.path.join(data_dir, "video_raw.csv"), index=False)
    personas.to_csv(os.path.join(data_dir, "persona_raw.csv"), index=False)
    print(f"video_raw.csv: {len(videos)} rows")
    print(f"persona_raw.csv: {len(personas)} rows")


if __name__ == "__main__":
    main()
