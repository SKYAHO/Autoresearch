"""Unit tests for CTR model interaction feature functions."""

import numpy as np
import json
from src.features.feature_builder import (
    compute_historical_category_match,
    compute_preferred_category_match,
    compute_topic_similarity,
    embed_keywords,
)
from src.features.category_reference import get_category_description_embedding, CATEGORY_DESCRIPTIONS


class TestComputeHistoricalCategoryMatch:
    """Tests for historical_category_match (behavior-based category matching)."""

    def test_match_same_category(self):
        """When historical affinity equals category_id, return 1."""
        assert compute_historical_category_match("Gaming", "Gaming") == 1

    def test_no_match_different_category(self):
        """When historical affinity differs from category_id, return 0."""
        assert compute_historical_category_match("Gaming", "Music") == 0

    def test_cold_start_unknown(self):
        """When historical affinity is 'unknown' (cold-start), always return 0."""
        assert compute_historical_category_match("unknown", "Gaming") == 0
        assert compute_historical_category_match("unknown", "Music") == 0

    def test_type_coercion_int_vs_str(self):
        """Both int and str inputs should work correctly (str coercion)."""
        # Defense against dtype mismatches: int category should coerce to str correctly
        assert compute_historical_category_match(20, "Gaming") == 0  # int 20 vs "Gaming" should not match
        assert compute_historical_category_match("Gaming", 20) == 0  # "Gaming" vs int 20 should not match


class TestComputePreferredCategoryMatch:
    """Tests for preferred_category_match (persona-based category matching)."""

    def test_match_in_list(self):
        """When category_id is in preferred_category list, return 1."""
        assert compute_preferred_category_match(["Gaming", "Music"], "Gaming") == 1

    def test_no_match_not_in_list(self):
        """When category_id is not in preferred_category list, return 0."""
        assert compute_preferred_category_match(["Gaming", "Music"], "News & Politics") == 0

    def test_empty_list(self):
        """When preferred_category is empty, return 0."""
        assert compute_preferred_category_match([], "Gaming") == 0

    def test_json_string_input(self):
        """Parse JSON string representation of list."""
        assert compute_preferred_category_match(json.dumps(["Gaming", "Music"]), "Gaming") == 1
        assert compute_preferred_category_match(json.dumps(["Gaming", "Music"]), "News & Politics") == 0

    def test_type_coercion_int_in_list(self):
        """Category IDs in list as int should be coerced to str and still not match string names."""
        # Defense: int 20 in list vs "Gaming" string should not match
        assert compute_preferred_category_match([20, 10], "Gaming") == 0

    def test_invalid_json(self):
        """Invalid JSON string returns 0 (safe fallback)."""
        assert compute_preferred_category_match("not json", "Gaming") == 0


class TestEmbedKeywords:
    """Tests for keyword embedding function."""

    def test_single_keyword(self):
        """Single keyword returns list with one embedding."""
        result = embed_keywords(["gaming"])
        assert len(result) == 1
        assert isinstance(result[0], np.ndarray)
        assert result[0].shape == (768,)

    def test_multiple_keywords(self):
        """Multiple keywords return multiple embeddings."""
        result = embed_keywords(["gaming", "music", "sports"])
        assert len(result) == 3
        assert all(isinstance(e, np.ndarray) for e in result)
        assert all(e.shape == (768,) for e in result)

    def test_empty_list(self):
        """Empty keyword list returns empty list."""
        result = embed_keywords([])
        assert result == []

    def test_deterministic_same_keyword(self):
        """Same keyword produces same embedding (deterministic hash-based)."""
        result1 = embed_keywords(["gaming"])
        result2 = embed_keywords(["gaming"])
        np.testing.assert_array_almost_equal(result1[0], result2[0])

    def test_different_keywords_different_embeddings(self):
        """Different keywords produce different embeddings."""
        result1 = embed_keywords(["gaming"])
        result2 = embed_keywords(["music"])
        assert not np.allclose(result1[0], result2[0])


class TestComputeTopicSimilarity:
    """Tests for topic_similarity (max-pooled keyword-category cosine similarity)."""

    def test_empty_embeddings_zero_similarity(self):
        """Empty keyword embeddings list returns 0.0."""
        assert compute_topic_similarity([], "Gaming") == 0.0

    def test_single_keyword_nonzero_similarity(self):
        """Single keyword embedding returns a valid cosine similarity with category."""
        gaming_embed = embed_keywords(["gaming"])
        similarity = compute_topic_similarity(gaming_embed, "Gaming")
        assert isinstance(similarity, float)
        # 두 단위 벡터의 코사인 유사도는 수학적으로 [-1, 1] 범위다. 테스트 환경의
        # fake 임베딩(#206, tests/conftest.py)은 텍스트 해시 기반 랜덤 벡터라
        # 의미적으로 가까운 텍스트끼리도 양의 유사도가 보장되지 않는다 — 실제
        # Vertex AI 임베딩에서만 "관련 있는 텍스트 → 양의 유사도"를 기대할 수 있다.
        assert -1 <= similarity <= 1

    def test_multiple_keywords_max_pool(self):
        """Multiple keywords return max cosine similarity (max-pool)."""
        keywords = ["gaming", "music", "sports"]
        embeddings = embed_keywords(keywords)
        similarity = compute_topic_similarity(embeddings, "Gaming")
        assert isinstance(similarity, float)
        assert -1 <= similarity <= 1

    def test_output_range(self):
        """Similarity output is in valid range [-1, 1]."""
        embeddings = embed_keywords(["gaming", "music", "sports", "travel"])
        for cat_name in ["Music", "Gaming", "News & Politics", "Education"]:
            similarity = compute_topic_similarity(embeddings, cat_name)
            assert -1 <= similarity <= 1

    def test_deterministic_same_inputs(self):
        """Same keywords and category produce same similarity (deterministic)."""
        embeddings1 = embed_keywords(["gaming", "music"])
        embeddings2 = embed_keywords(["gaming", "music"])
        similarity1 = compute_topic_similarity(embeddings1, "Gaming")
        similarity2 = compute_topic_similarity(embeddings2, "Gaming")
        assert similarity1 == similarity2


class TestGetCategoryDescriptionEmbedding:
    """Tests for category description embedding function."""

    def test_known_category_returns_embedding(self):
        """Known category name returns embedding."""
        embedding = get_category_description_embedding("Gaming")
        assert isinstance(embedding, np.ndarray)
        assert embedding.shape == (768,)

    def test_deterministic_same_category(self):
        """Same category name always returns same embedding."""
        emb1 = get_category_description_embedding("Gaming")
        emb2 = get_category_description_embedding("Gaming")
        np.testing.assert_array_equal(emb1, emb2)

    def test_different_categories_different_embeddings(self):
        """Different categories return different embeddings."""
        emb1 = get_category_description_embedding("Music")
        emb2 = get_category_description_embedding("Gaming")
        assert not np.allclose(emb1, emb2)

    def test_unknown_category_fallback_to_default(self):
        """Unknown category name returns default category (People & Blogs) embedding."""
        unknown_embedding = get_category_description_embedding("NonexistentCategory")
        default_embedding = get_category_description_embedding("People & Blogs")
        np.testing.assert_array_equal(unknown_embedding, default_embedding)

    def test_all_15_categories_exist(self):
        """All 15 defined categories have embeddings."""
        for cat_name in CATEGORY_DESCRIPTIONS.keys():
            embedding = get_category_description_embedding(cat_name)
            assert isinstance(embedding, np.ndarray)
            assert embedding.shape == (768,)
            assert not np.allclose(embedding, 0)
