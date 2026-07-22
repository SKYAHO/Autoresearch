"""scripts/build_static_features.py의 순수 로직(SQL 빌더·키워드 추출) 테스트.

BigQuery/Vertex AI 호출은 대상이 아니다. dataset 계층 분리, TRUNCATE+INSERT
사용, 누락 방지 grid, event_timestamp 고정 같은 계약만 검증한다.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import numpy as np
import pandas as pd
import pytest


_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "build_static_features.py"
_spec = importlib.util.spec_from_file_location("build_static_features", _SCRIPT)
bsf = importlib.util.module_from_spec(_spec)
# dataclass 처리가 sys.modules[cls.__module__]를 조회하므로 exec 전에 등록한다.
sys.modules["build_static_features"] = bsf
_spec.loader.exec_module(bsf)


def _settings(**overrides):
    base = dict(
        project="p",
        feature_dataset="feast_offline_store",
        embedding_dataset="autoresearch_dev_analytics",
        location="asia-northeast3",
        bucket="b",
        persona_path="asset/virtual_user/vu_1000.parquet",
        dry_run=False,
        cache_dir=None,
    )
    base.update(overrides)
    return bsf.Settings(**base)


def test_embedding_tables_are_not_in_feature_dataset() -> None:
    # 임베딩 중간 산출물은 Feast feature dataset이 아니라 analytics dataset에.
    assert bsf.DEFAULT_EMBEDDING_DATASET == "autoresearch_dev_analytics"
    assert bsf.DEFAULT_FEATURE_DATASET == "feast_offline_store"
    assert bsf.DEFAULT_EMBEDDING_DATASET != bsf.DEFAULT_FEATURE_DATASET


def test_user_static_sql_truncates_and_reads_external_persona() -> None:
    sql = bsf.user_static_feature_sql(_settings())
    assert sql.startswith(
        "TRUNCATE TABLE `p.feast_offline_store.user_static_feature`;"
    )
    assert "INSERT INTO `p.feast_offline_store.user_static_feature`" in sql
    # GCS external table alias만 읽고, 삭제 예정 BQ 테이블은 참조하지 않는다.
    assert "FROM persona" in sql
    assert "asset_virtual_user_vu_1000" not in sql
    # terraform 소유 스키마를 덮어쓰는 구문 금지.
    assert "CREATE OR REPLACE" not in sql
    assert "WRITE_TRUNCATE" not in sql
    # 정적 feature timestamp 고정.
    assert "TIMESTAMP '1970-01-01 00:00:00 UTC'" in sql


def test_similarity_sql_uses_cross_dataset_sources_and_truncate_insert() -> None:
    sql = bsf.user_category_similarity_sql(_settings())
    assert sql.count("TRUNCATE TABLE `p.feast_offline_store.user_category_similarity`;") == 1
    assert "INSERT INTO `p.feast_offline_store.user_category_similarity`" in sql
    # 임베딩 원본은 analytics dataset에서 읽는다.
    assert "`p.autoresearch_dev_analytics.category_embedding`" in sql
    assert "`p.autoresearch_dev_analytics.user_topic_embedding`" in sql
    assert "CREATE OR REPLACE" not in sql


def test_similarity_sql_uses_ml_distance_not_unnest_explosion() -> None:
    sql = bsf.user_category_similarity_sql(_settings())
    # 768차원을 행으로 펼치면 중간 결과가 16억 행이 되어 사실상 끝나지 않는다.
    assert "ML.DISTANCE(" in sql
    assert "'COSINE'" in sql
    assert "WITH OFFSET" not in sql
    assert "CROSS JOIN UNNEST" not in sql


def test_similarity_sql_grid_left_join_prevents_dropped_users() -> None:
    sql = bsf.user_category_similarity_sql(_settings())
    # 전체 유저 × 전체 카테고리 grid에 LEFT JOIN → 키워드 없는 유저도 포함.
    assert "CROSS JOIN categories c" in sql
    assert "FROM grid g" in sql
    assert "LEFT JOIN best b" in sql
    # 매칭 없는 경우 문서 규칙 6대로 0.0 / 'unknown'.
    assert "COALESCE(b.cosine_score, 0.0) AS topic_similarity" in sql
    assert "COALESCE(b.topic, 'unknown') AS topic_similarity_top_topic" in sql
    # user grid는 전체 유저 테이블에서 온다(임베딩 테이블이 아니라).
    assert "FROM `p.feast_offline_store.user_static_feature`" in sql


def test_similarity_sql_pins_static_timestamp_and_our_model() -> None:
    sql = bsf.user_category_similarity_sql(_settings())
    assert "TIMESTAMP '1970-01-01 00:00:00 UTC' AS event_timestamp" in sql
    # 우리가 실제로 쓰는 임베딩 모델/차원.
    assert "'text-multilingual-embedding-002' AS embedding_model" in sql
    assert "768 AS embedding_dim" in sql


def test_extract_topic_rows_explodes_and_preserves_source() -> None:
    df = pd.DataFrame(
        [
            {
                "user_id": "vu_0001",
                "hobby_keywords": ["등산", "캠핑"],
                "interest_keywords": ["넷플릭스"],
                "lifestyle_keywords": None,
                "food_keywords": [],
                "travel_keywords": ["경주"],
                "career_keywords": [],
                "family_context_keywords": [],
            }
        ]
    )
    rows = bsf.extract_topic_rows(df)
    assert {(r["topic"], r["topic_source"]) for r in rows} == {
        ("등산", "hobby_keywords"),
        ("캠핑", "hobby_keywords"),
        ("넷플릭스", "interest_keywords"),
        ("경주", "travel_keywords"),
    }
    assert all(r["user_id"] == "vu_0001" for r in rows)


def test_extract_topic_rows_dedups_and_skips_blank_and_missing_user() -> None:
    df = pd.DataFrame(
        [
            {
                "user_id": "vu_0001",
                "hobby_keywords": ["등산", "등산", "  ", None],
                "interest_keywords": [],
                "lifestyle_keywords": [],
                "food_keywords": [],
                "travel_keywords": [],
                "career_keywords": [],
                "family_context_keywords": [],
            },
            {
                "user_id": None,  # user_id 없는 행은 건너뛴다.
                "hobby_keywords": ["무시됨"],
                "interest_keywords": [],
                "lifestyle_keywords": [],
                "food_keywords": [],
                "travel_keywords": [],
                "career_keywords": [],
                "family_context_keywords": [],
            },
        ]
    )
    rows = bsf.extract_topic_rows(df)
    assert rows == [{"user_id": "vu_0001", "topic": "등산", "topic_source": "hobby_keywords"}]


def test_unique_topics_preserves_order_without_duplicates() -> None:
    rows = [
        {"topic": "등산"},
        {"topic": "캠핑"},
        {"topic": "등산"},
        {"topic": "경주"},
    ]
    assert bsf.unique_topics(rows) == ["등산", "캠핑", "경주"]


def test_select_steps_orders_by_dependency_and_rejects_unknown() -> None:
    # 요청 순서와 무관하게 의존 순서(STEPS)로 정렬한다.
    assert bsf.select_steps("user_category_similarity,user_static_feature") == [
        "user_static_feature",
        "user_category_similarity",
    ]
    assert bsf.select_steps(None) == list(bsf.STEPS)
    with pytest.raises(ValueError):
        bsf.select_steps("nope")


def test_embed_reuses_cache_and_only_calls_api_for_new_topics(monkeypatch, tmp_path):
    calls: list[list[str]] = []

    def _fake_embed_texts(texts, task_type):
        calls.append(list(texts))
        return [np.full(bsf.EMBEDDING_DIM, float(len(t))) for t in texts]

    import types

    fake_module = types.ModuleType("src.features.embeddings")
    fake_module.embed_texts = _fake_embed_texts
    monkeypatch.setitem(sys.modules, "src.features.embeddings", fake_module)
    monkeypatch.setattr(bsf, "EMBED_SLICE_PAUSE_SEC", 0)

    cache_path = tmp_path / "cache.jsonl"
    settings = _settings(cache_dir=tmp_path)

    first = bsf._embed(["a", "bb"], "RETRIEVAL_QUERY", settings, cache_path)
    assert calls == [["a", "bb"]]
    assert cache_path.exists()

    # 두 번째 호출은 캐시에 없는 topic만 API로 보낸다.
    second = bsf._embed(["a", "bb", "ccc"], "RETRIEVAL_QUERY", settings, cache_path)
    assert calls[1] == ["ccc"]
    assert second[:2] == first
    assert len(second) == 3


def test_embed_slices_requests_to_respect_quota(monkeypatch, tmp_path):
    calls: list[int] = []

    def _fake_embed_texts(texts, task_type):
        calls.append(len(texts))
        return [np.zeros(bsf.EMBEDDING_DIM) for _ in texts]

    import types

    fake_module = types.ModuleType("src.features.embeddings")
    fake_module.embed_texts = _fake_embed_texts
    monkeypatch.setitem(sys.modules, "src.features.embeddings", fake_module)
    monkeypatch.setattr(bsf, "EMBED_SLICE_SIZE", 2)
    monkeypatch.setattr(bsf, "EMBED_SLICE_PAUSE_SEC", 0)

    bsf._embed(["a", "b", "c", "d", "e"], "RETRIEVAL_QUERY", _settings(), None)

    # 한 번에 몰아 보내지 않고 슬라이스 단위로 끊어 호출한다.
    assert calls == [2, 2, 1]


def test_embed_dry_run_does_not_call_api(monkeypatch):
    def _boom(*_args, **_kwargs):
        raise AssertionError("dry-run은 임베딩 API를 호출하면 안 된다")

    import types

    fake_module = types.ModuleType("src.features.embeddings")
    fake_module.embed_texts = _boom
    monkeypatch.setitem(sys.modules, "src.features.embeddings", fake_module)

    vectors = bsf._embed(["a"], "RETRIEVAL_QUERY", _settings(dry_run=True), None)
    assert vectors == [[0.0] * bsf.EMBEDDING_DIM]


def test_embedding_cache_is_append_only_json_lines(monkeypatch, tmp_path):
    """캐시는 슬라이스마다 append만 한다 — 전량 재작성은 O(n^2)라 46k건에서 못 쓴다."""

    def _fake_embed_texts(texts, task_type):
        return [np.zeros(bsf.EMBEDDING_DIM) for _ in texts]

    import types

    fake_module = types.ModuleType("src.features.embeddings")
    fake_module.embed_texts = _fake_embed_texts
    monkeypatch.setitem(sys.modules, "src.features.embeddings", fake_module)
    monkeypatch.setattr(bsf, "EMBED_SLICE_SIZE", 2)
    monkeypatch.setattr(bsf, "EMBED_SLICE_PAUSE_SEC", 0)

    cache_path = tmp_path / "cache.jsonl"
    bsf._embed(["a", "b", "c"], "RETRIEVAL_QUERY", _settings(), cache_path)

    lines = cache_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines) == 3
    assert {json.loads(line)["topic"] for line in lines} == {"a", "b", "c"}

    # 이어서 호출하면 새 topic만 append되고 기존 줄은 그대로 남는다.
    bsf._embed(["a", "d"], "RETRIEVAL_QUERY", _settings(), cache_path)
    lines_after = cache_path.read_text(encoding="utf-8").strip().splitlines()
    assert len(lines_after) == 4
    assert lines_after[:3] == lines


def test_embedding_cache_skips_corrupt_trailing_line(tmp_path):
    """쓰다가 죽어 마지막 줄이 잘려도 나머지 캐시는 살린다."""

    cache_path = tmp_path / "cache.jsonl"
    cache_path.write_text(
        json.dumps({"topic": "a", "vector": [0.1]}) + "\n" + '{"topic": "b", "vec',
        encoding="utf-8",
    )
    assert bsf._load_embedding_cache(cache_path) == {"a": [0.1]}
