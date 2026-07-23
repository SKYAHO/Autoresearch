"""BQ 가상 유저 → 학습 계약 personas 어댑터 단위 테스트 (v2 모델 결함 회귀 테스트)."""

import json

import numpy as np
import pandas as pd

from src.pipeline.build_training_dataset import load_personas
from src.pipeline.virtual_user_adapter import extract_words, to_personas_frame


def test_extract_words_handles_bq_arrow_nested_structure():
    # 2026-07-21 실측 구조: 중첩 dict를 그대로 순회하면 키 "list"만 나와 키워드가 붕괴한다.
    nested = {"list": np.array([{"element": "노포 맛집 탐방"}, {"element": "리그 오브 레전드"}], dtype=object)}
    assert extract_words(nested) == ["노포 맛집 탐방", "리그 오브 레전드"]


def test_extract_words_handles_flat_and_empty_inputs():
    assert extract_words(["글램핑", "드라이브"]) == ["글램핑", "드라이브"]
    assert extract_words(np.array(["산책"], dtype=object)) == ["산책"]
    assert extract_words(None) == []
    assert extract_words({"list": np.array([], dtype=object)}) == []
    assert extract_words(123) == []  # 비순회 스칼라는 빈 목록


def test_to_personas_frame_builds_training_contract_columns():
    vu = pd.DataFrame(
        {
            "user_id": ["vu_0001"],
            "age": [24],
            "occupation": ["대학생"],
            "hobby_keywords": [{"list": np.array([{"element": "게임"}], dtype=object)}],
            "interest_keywords": [["e스포츠"]],
            "lifestyle_keywords": [None],
            "watch_time_band": ["night"],
        }
    )
    personas = to_personas_frame(vu)
    assert list(personas.columns) == [
        "uuid", "age", "occupation", "hobbies_and_interests_list", "hobbies_and_interests",
        "watch_time_band",
    ]
    row = personas.iloc[0]
    assert row.uuid == "vu_0001"
    assert json.loads(row.hobbies_and_interests_list) == ["게임", "e스포츠"]
    assert row.hobbies_and_interests == "게임, e스포츠"
    assert row.watch_time_band == "night"


def test_to_personas_frame_keeps_users_with_no_keywords():
    vu = pd.DataFrame(
        {
            "user_id": ["vu_0002"],
            "age": [50],
            "occupation": ["자영업"],
            "hobby_keywords": [None],
            "interest_keywords": [None],
            "lifestyle_keywords": [None],
        }
    )
    personas = to_personas_frame(vu)
    assert json.loads(personas.iloc[0].hobbies_and_interests_list) == []
    assert personas.iloc[0].hobbies_and_interests == ""


def test_load_personas_preserves_watch_time_band_from_parquet(tmp_path):
    source = tmp_path / "virtual_users.parquet"
    pd.DataFrame(
        {
            "user_id": ["vu_0003"],
            "age": [37],
            "occupation": ["개발자"],
            "hobby_keywords": [[]],
            "interest_keywords": [[]],
            "lifestyle_keywords": [[]],
            "watch_time_band": ["evening"],
        }
    ).to_parquet(source)

    personas = load_personas(str(source))

    assert personas.iloc[0].watch_time_band == "evening"
