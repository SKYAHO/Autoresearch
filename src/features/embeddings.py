"""Shared embedding utility functions for CTR model feature engineering.

PLACEHOLDER: Text encoding using deterministic hash-based pseudo-embeddings.
Real implementation requires Sentence Transformer (e.g., all-MiniLM-L6-v2).
See: CTR_Model_Specification.md (Intermediate Artifacts section)
"""

import hashlib
import numpy as np


def embed_text(text: str, dim: int = 32) -> np.ndarray:
    """Encode text to fixed-dimension vector using deterministic pseudo-embedding.

    PLACEHOLDER: This implementation uses hash-based deterministic encoding.
    In real system, replace with Sentence Transformer:
        from sentence_transformers import SentenceTransformer
        model = SentenceTransformer("all-MiniLM-L6-v2")
        return model.encode(text)

    Args:
        text: Input string to embed.
        dim: Output vector dimension (default 32).

    Returns:
        L2-normalized embedding vector (shape: dim,).
    """
    h = hashlib.sha256(text.encode("utf-8")).digest()
    rng = np.random.default_rng(int.from_bytes(h[:8], "big"))
    v = rng.normal(size=dim)
    return v / np.linalg.norm(v)


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    """Compute cosine similarity between two normalized vectors.

    Args:
        a: First embedding vector (assumed L2-normalized).
        b: Second embedding vector (assumed L2-normalized).

    Returns:
        Cosine similarity (range: -1 to 1, typical [0, 1] for normalized vectors).
    """
    return float(np.dot(a, b))
