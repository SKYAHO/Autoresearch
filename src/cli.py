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
) -> None:
    """training_dataset.csv 생성."""
    build_training_dataset.main(raw_dir=raw_dir, events_path=events_path, output_path=output_path)


@app.command()
def train_model(
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    data_path: Optional[str] = typer.Option(None, help="training dataset 경로 (config override)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (config override)"),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    random_state: Optional[int] = typer.Option(None, help="Random state (config override)"),
) -> None:
    """LightGBM 모델 훈련 (train/val/test 3-way split, test는 완전 held-out)."""
    train.main(
        config_path=config_path,
        data_path=data_path,
        model_output=model_output,
        test_size=test_size,
        random_state=random_state,
    )


@app.command()
def evaluate_model(
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    data_path: Optional[str] = typer.Option(None, help="평가용 데이터 경로 (config override, 기본: held-out test set)"),
    model_path: Optional[str] = typer.Option(None, help="모델 로드 경로 (config override)"),
) -> None:
    """저장된 모델을 held-out test set으로 평가."""
    evaluate.main(config_path=config_path, data_path=data_path, model_path=model_path)


@app.command()
def run_pipeline(
    raw_dir: Optional[str] = typer.Option(None, help="Raw 데이터 디렉토리 (기본: data/raw)"),
    events_path: Optional[str] = typer.Option(None, help="Event log CSV 경로 (기본: data/processed/events.csv)"),
    dataset_path: Optional[str] = typer.Option(None, help="Training dataset 경로 (기본: data/processed/training_dataset.csv)"),
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (config override)"),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    random_state: Optional[int] = typer.Option(None, help="Random state (config override)"),
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
        test_size=test_size,
        random_state=random_state,
    )

    # data_path는 넘기지 않는다: evaluate는 train-model이 분리해 저장한 held-out
    # test set(config의 artifacts.test_set_path)으로 채점해야 하며, 방금 만든
    # dataset_path(train+val+test 전체)를 그대로 넘기면 data leakage가 재발한다.
    typer.echo("\n[3/3] evaluate-model 실행...")
    evaluate.main(config_path=config_path, data_path=None, model_path=model_output)

    typer.echo("\n" + "=" * 70)
    typer.echo("파이프라인 완료")
    typer.echo("=" * 70)


if __name__ == "__main__":
    app()
