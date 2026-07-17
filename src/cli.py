#!/usr/bin/env python3
"""LightGBM 학습 파이프라인 Typer CLI.

python -m src.cli build-features / train-model / evaluate-model / run-pipeline
"""

import os
import sys
from typing import Optional

import typer

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJECT_ROOT)

from src.pipeline import build_training_dataset, train, evaluate  # noqa: E402

app = typer.Typer()


@app.command()
def build_features(
    raw_dir: Optional[str] = typer.Option(None, help="Raw 데이터 디렉토리 (기본: data/raw)"),
    events_path: Optional[str] = typer.Option(None, help="Event log CSV 경로 (기본: data/processed/events.csv)"),
    output_path: Optional[str] = typer.Option(None, help="출력 CSV 경로 (기본: data/processed/training_dataset.csv)"),
    videos_source: str = typer.Option(
        "csv", help="videos 입력 소스: csv(mock) 또는 bigquery(data_lake_youtube_trending_kr)"
    ),
    personas_path: Optional[str] = typer.Option(
        None, help="Persona 파일 경로(로컬 CSV 또는 gs://.../*.parquet, 기본: <raw_dir>/personas.csv)"
    ),
) -> None:
    """training_dataset.csv 생성."""
    build_training_dataset.main(
        raw_dir=raw_dir,
        events_path=events_path,
        output_path=output_path,
        videos_source=videos_source,
        personas_path=personas_path,
    )


@app.command()
def train_model(
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    data_path: Optional[str] = typer.Option(None, help="training dataset 경로 (config override)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (config override)"),
    test_set_output: Optional[str] = typer.Option(
        None, help="Held-out test set 저장 경로 (config override, 병렬 실험 시 실험별로 분리 필요)"
    ),
    feature_columns_output: Optional[str] = typer.Option(None, help="Feature 목록 저장 경로 (config override)"),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    val_size: Optional[float] = typer.Option(None, help="Val set 비율 (config override)"),
    random_state: Optional[int] = typer.Option(None, help="Random state (config override, 데이터 split과 모델 둘 다 적용)"),
) -> None:
    """LightGBM 모델 훈련 (train/val/test 3-way split, test는 완전 held-out)."""
    train.main(
        config_path=config_path,
        data_path=data_path,
        model_output=model_output,
        test_set_output=test_set_output,
        feature_columns_output=feature_columns_output,
        test_size=test_size,
        val_size=val_size,
        random_state=random_state,
    )


@app.command()
def evaluate_model(
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    data_path: Optional[str] = typer.Option(None, help="평가용 데이터 경로 (config override, 기본: held-out test set)"),
    model_path: Optional[str] = typer.Option(None, help="모델 로드 경로 (config override)"),
    feature_columns_path: Optional[str] = typer.Option(None, help="Feature 목록 경로 (config override)"),
) -> None:
    """저장된 모델을 held-out test set으로 평가."""
    evaluate.main(
        config_path=config_path,
        data_path=data_path,
        model_path=model_path,
        feature_columns_path=feature_columns_path,
    )


@app.command()
def run_pipeline(
    raw_dir: Optional[str] = typer.Option(None, help="Raw 데이터 디렉토리 (기본: data/raw)"),
    events_path: Optional[str] = typer.Option(None, help="Event log CSV 경로 (기본: data/processed/events.csv)"),
    dataset_path: Optional[str] = typer.Option(None, help="Training dataset 경로 (기본: data/processed/training_dataset.csv)"),
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (config override)"),
    test_set_output: Optional[str] = typer.Option(
        None, help="Held-out test set 저장 경로 (config override, 병렬 실험 시 실험별로 분리 필요)"
    ),
    feature_columns_output: Optional[str] = typer.Option(None, help="Feature 목록 저장 경로 (config override)"),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    val_size: Optional[float] = typer.Option(None, help="Val set 비율 (config override)"),
    random_state: Optional[int] = typer.Option(None, help="Random state (config override, 데이터 split과 모델 둘 다 적용)"),
) -> None:
    """전체 파이프라인 실행: build-features -> train-model -> evaluate-model."""
    typer.echo("=" * 70)
    typer.echo("전체 파이프라인 실행")
    typer.echo("=" * 70)

    typer.echo("\n[1/3] build-features 실행...")
    build_training_dataset.main(raw_dir=raw_dir, events_path=events_path, output_path=dataset_path)

    typer.echo("\n[2/3] train-model 실행...")
    train.main(
        config_path=config_path,
        data_path=dataset_path,
        model_output=model_output,
        test_set_output=test_set_output,
        feature_columns_output=feature_columns_output,
        test_size=test_size,
        val_size=val_size,
        random_state=random_state,
    )

    # dataset_path(방금 만든 train+val+test 전체)는 넘기지 않는다: evaluate는
    # train-model이 분리해 저장한 held-out test set으로만 채점해야 하며, 그대로
    # 넘기면 data leakage가 재발한다. 대신 test_set_output/feature_columns_output을
    # 그대로 전달해서, 병렬로 여러 run-pipeline을 돌릴 때도(각자 다른 경로를 줬다면)
    # 자기 자신이 만든 test set/feature 목록으로 채점되도록 짝을 맞춘다.
    typer.echo("\n[3/3] evaluate-model 실행...")
    evaluate.main(
        config_path=config_path,
        data_path=test_set_output,
        model_path=model_output,
        feature_columns_path=feature_columns_output,
    )

    typer.echo("\n" + "=" * 70)
    typer.echo("파이프라인 완료")
    typer.echo("=" * 70)


if __name__ == "__main__":
    app()
