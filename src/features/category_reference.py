"""Category reference data and embeddings for CTR model feature engineering.

Implements 15 fixed YouTube categories with descriptions and embeddings.
See: CTR_Model_Specification.md (Intermediate Artifacts section)

NOTE: category_description_embedding는 모듈 import 시 1회만 생성되어 캐시됨.
"""

from typing import Optional
import numpy as np

from src.features.embeddings import embed_text


CATEGORY_DESCRIPTIONS = {
    "Film & Animation": "이 카테고리는 영화·애니메이션 콘텐츠입니다. 영화 리뷰, 애니메이션, 단편영화, 영화 해금 토론을 다룹니다.",
    "Autos & Vehicles": "이 카테고리는 자동차·오토바이 콘텐츠입니다. 차량 리뷰, 정비기, 튜닝, 드라이브 블로그를 다룹니다.",
    "Music": "이 카테고리는 음악 콘텐츠입니다. 뮤직비디오, 커버곡, 라이브 공연, K-POP을 다룹니다.",
    "Pets & Animals": "이 카테고리는 반려동물 콘텐츠입니다. 강아지, 고양이, 동물 블로그, 반려동물 훈련을 다룹니다.",
    "Sports": "이 카테고리는 스포츠 콘텐츠입니다. 축구, 야구, 농구, 올림픽, 경기 하이라이트를 다룹니다.",
    "Travel & Events": "이 카테고리는 여행·행사 콘텐츠입니다. 국내여행, 해외여행, 여행 블로그, 축제, 캠핑을 다룹니다.",
    "Gaming": "이 카테고리는 게임 콘텐츠입니다. 롤(리그오브레전드), 배틀그라운드, 게임 공략, e스포츠, 게임 스트리밍을 다룹니다.",
    "People & Blogs": "이 카테고리는 일상 블로그 콘텐츠입니다. 개인방송, 일상 공유, 일대기를 다룹니다.",
    "Comedy": "이 카테고리는 코미디 콘텐츠입니다. 개그, 코윤, 패러디, 상황극, 몰래카메라를 다룹니다.",
    "Entertainment": "이 카테고리는 예능 콘텐츠입니다. 오디션, 챌린지, 리액션, 버라이어티를 다룹니다.",
    "News & Politics": "이 카테고리는 뉴스·정치 콘텐츠입니다. 뉴스, 정치 이슈, 시사 토론을 다룹니다.",
    "Howto & Style": "이 카테고리는 뷰티·라이프스타일 콘텐츠입니다. 메이크업, 패션, DIY, 요리교법을 다룹니다.",
    "Education": "이 카테고리는 교육 콘텐츠입니다. 강의, 학습법, 자격증 준비, 언어학습을 다룹니다.",
    "Science & Technology": "이 카테고리는 과학·기술 콘텐츠입니다. IT, 프로그래밍, 과학실험, 전기술, 전자기기 리뷰를 다룹니다.",
    "Nonprofits & Activism": "이 카테고리는 사회활동 콘텐츠입니다. 사회활동, 환경운동, 사회이슈 캠페인을 다룹니다.",
}

_CATEGORY_EMBEDDINGS = {}


def _init_category_embeddings() -> None:
    """Initialize embeddings for all 15 categories on module load."""
    global _CATEGORY_EMBEDDINGS
    if not _CATEGORY_EMBEDDINGS:
        for cat_id, desc in CATEGORY_DESCRIPTIONS.items():
            _CATEGORY_EMBEDDINGS[cat_id] = embed_text(desc)


def get_category_description_embedding(category_id: str) -> np.ndarray:
    """Get embedding for given category name, with fallback to default category (People & Blogs).

    Args:
        category_id: YouTube category name (string, e.g., "Gaming").

    Returns:
        Embedding vector (L2-normalized).
    """
    if not _CATEGORY_EMBEDDINGS:
        _init_category_embeddings()

    if category_id in _CATEGORY_EMBEDDINGS:
        return _CATEGORY_EMBEDDINGS[category_id]

    return _CATEGORY_EMBEDDINGS.get("People & Blogs", _CATEGORY_EMBEDDINGS["People & Blogs"])


_init_category_embeddings()
