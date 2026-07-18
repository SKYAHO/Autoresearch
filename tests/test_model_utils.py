from __future__ import annotations

from pathlib import Path

from src.utils.model_utils import load_categorical_columns, save_categorical_columns


def test_categorical_columns_roundtrip(tmp_path: Path) -> None:
    categories_by_column = {
        "category_id": [10, 20, 30],
        "age_group": ["10s", "20s", "30s"],
    }
    path = tmp_path / "categorical_columns.pkl"

    save_categorical_columns(categories_by_column, str(path))
    loaded = load_categorical_columns(str(path))

    assert loaded == categories_by_column
