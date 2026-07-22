"""BQ 가상 유저 테이블을 학습·피처 조립 계약의 personas DataFrame으로 변환한다.

BigQuery REPEATED 컬럼은 클라이언트에서 Arrow 중첩 구조
({'list': [{'element': str}, ...]})로 내려올 수 있다. 이를 그대로 순회하면
dict 키("list")만 추출되어 전 키워드가 붕괴한다 — 2026-07-21 v2 모델 결함의
원인이었으며, 이 모듈이 그 정규화를 단일 책임으로 가진다.
"""

from __future__ import annotations

__arch__ = {
    "stage": "training",
    "role": "BQ 가상 유저 테이블을 학습 계약 personas 형태로 정규화합니다.",
    "owns": [
        "BQ Arrow 중첩 배열 키워드 추출",
        "personas 계약 컬럼(uuid·age·occupation·관심사) 조립",
    ],
    "not_owns": [
        "피처 계산",
        "BigQuery 조회 실행",
    ],
}

import json

import pandas as pd

_KEYWORD_COLUMNS = ("hobby_keywords", "interest_keywords", "lifestyle_keywords")


def extract_words(value: object) -> list[str]:
    """키워드 컬럼 값에서 단어 목록을 추출한다.

    지원 형태: BQ Arrow 중첩({'list': [{'element': str}, ...]}), 평평한
    시퀀스(list/ndarray), None. 그 외 비순회 값은 빈 목록으로 취급한다.
    """
    if value is None:
        return []
    if isinstance(value, dict) and "list" in value:
        words: list[str] = []
        for entry in value["list"]:
            word = entry.get("element") if isinstance(entry, dict) else entry
            if word:
                words.append(str(word))
        return words
    if isinstance(value, str):
        return [value] if value else []
    try:
        return [str(word) for word in value if word]
    except TypeError:
        return []


def to_personas_frame(virtual_users: pd.DataFrame) -> pd.DataFrame:
    """가상 유저 테이블을 personas 계약(uuid/age/occupation/관심사 2형)으로 변환한다."""
    word_lists = virtual_users.apply(
        lambda row: [
            word for column in _KEYWORD_COLUMNS for word in extract_words(row[column])
        ],
        axis=1,
    )
    return pd.DataFrame(
        {
            "uuid": virtual_users["user_id"].astype(str),
            "age": virtual_users["age"],
            "occupation": virtual_users["occupation"],
            "hobbies_and_interests_list": word_lists.apply(
                lambda words: json.dumps(words, ensure_ascii=False)
            ),
            "hobbies_and_interests": word_lists.apply(", ".join),
        }
    )
