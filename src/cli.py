#!/usr/bin/env python3
"""Typer CLI for LightGBM training pipeline."""

import typer
from typing import Optional

from src.pipeline import build_training_dataset, train, evaluate

app = typer.Typer()


@app.command()
def build_features(
    raw_dir: str = typer.Option("data/raw", help="Raw 데이터 디렉토리"),
    events_path: str = typer.Option("data/processed/events.csv", help="Event log CSV 경로"),
    output_path: str = typer.Option("data/processed/training_dataset.csv", help="출력 CSV 경로"),
) -> None:
    """training_dataset.csv 생성."""
    build_training_dataset.main(raw_dir=raw_dir, events_path=events_path, output_path=output_path)


@app.command()
def train_model(
    config_path: str = typer.Option("src/pipeline/config.yaml", help="config.yaml 경로"),
    data_path: Optional[str] = typer.Option(None, help="training dataset 경로 (override config)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (override config)"),
    test_size: Optional[float] = typer.Option(None, help="Test set ratio (override config)"),
    random_state: Optional[int] = typer.Option(None, help="Random state (override config)"),
) -> None:
    """LightGBM 모델 훈련."""
    train.main(
        config_path=config_path,
        data_path=data_path,
        model_output=model_output,
        test_size=test_size,
        random_state=random_state,
    )


@app.command()
def evaluate_model(
    config_path: str = typer.Option("src/pipeline/config.yaml", help="config.yaml 경로"),
    data_path: Optional[str] = typer.Option(None, help="Dataset 경로 (override config)"),
    model_path: Optional[str] = typer.Option(None, help="모델 로드 경로 (override config)"),
) -> None:
    """모델 평가."""
    evaluate.main(config_path=config_path, data_path=data_path, model_path=model_path)


@app.command()
def run_pipeline(
    raw_dir: str = typer.Option("data/raw", help="Raw 데이터 디렉토리"),
    events_path: str = typer.Option("data/processed/events.csv", help="Event log CSV 경로"),
    dataset_path: str = typer.Option("data/processed/training_dataset.csv", help="Training dataset 경로"),
    config_path: str = typer.Option("src/pipeline/config.yaml", help="config.yaml 경로"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (override config)"),
    test_size: Optional[float] = typer.Option(None, help="Test set ratio (override config)"),
    random_state: Optional[int] = typer.Option(None, help="Random state (override config)"),
) -> None:
    """전체 파이프라인 실행: build-features → train → evaluate."""
    typer.echo("=" * 70)
    typer.echo("전체 파이프라인 실행")
    typer.echo("=" * 70)

    typer.echo("\n[1/3] build-features 실행...")
    build_training_dataset.main(raw_dir=raw_dir, events_path=events_path, output_path=dataset_path)

    typer.echo("\n[2/3] train 실행...")
    train.main(
        config_path=config_path,
        data_path=dataset_path,
        model_output=model_output,
        test_size=test_size,
        random_state=random_state,
    )

    typer.echo("\n[3/3] evaluate 실행...")
    evaluate.main(config_path=config_path, data_path=dataset_path, model_path=model_output)

    typer.echo("\n" + "=" * 70)
    typer.echo("파이프라인 완료")
    typer.echo("=" * 70)


if __name__ == "__main__":
    app()
