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
from src.tracking import promote  # noqa: E402

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
    events_source: str = typer.Option(
        "csv", help="events 입력 소스: csv(mock) 또는 bigquery(data_lake_action_log)"
    ),
    events_start_date: Optional[str] = typer.Option(
        None, help="events-source bigquery일 때 학습 기간 시작일(YYYY-MM-DD)"
    ),
    events_end_date: Optional[str] = typer.Option(
        None, help="events-source bigquery일 때 학습 기간 종료일(YYYY-MM-DD)"
    ),
    topic_similarity_source: str = typer.Option(
        "inmemory",
        help="topic_similarity 소스: inmemory(Vertex AI 즉석 계산) 또는 "
        "bigquery(feast_offline_store.user_category_similarity 사전 계산값 조회, #214/#244)",
    ),
) -> None:
    """training_dataset.csv 생성."""
    build_training_dataset.main(
        raw_dir=raw_dir,
        events_path=events_path,
        output_path=output_path,
        videos_source=videos_source,
        personas_path=personas_path,
        events_source=events_source,
        events_start_date=events_start_date,
        events_end_date=events_end_date,
        topic_similarity_source=topic_similarity_source,
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
    categorical_columns_output: Optional[str] = typer.Option(None, help="Categorical 카테고리 저장 경로 (config override)"),
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
        categorical_columns_output=categorical_columns_output,
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
    videos_source: str = typer.Option(
        "csv", help="videos 입력 소스: csv(mock) 또는 bigquery(data_lake_youtube_trending_kr)"
    ),
    personas_path: Optional[str] = typer.Option(
        None, help="Persona 파일 경로(로컬 CSV 또는 gs://.../*.parquet, 기본: <raw_dir>/personas.csv)"
    ),
    events_source: str = typer.Option(
        "csv", help="events 입력 소스: csv(mock) 또는 bigquery(data_lake_action_log)"
    ),
    events_start_date: Optional[str] = typer.Option(
        None, help="events-source bigquery일 때 학습 기간 시작일(YYYY-MM-DD)"
    ),
    events_end_date: Optional[str] = typer.Option(
        None, help="events-source bigquery일 때 학습 기간 종료일(YYYY-MM-DD)"
    ),
    topic_similarity_source: str = typer.Option(
        "inmemory",
        help="topic_similarity 소스: inmemory(Vertex AI 즉석 계산) 또는 "
        "bigquery(feast_offline_store.user_category_similarity 사전 계산값 조회, #214/#244)",
    ),
    config_path: Optional[str] = typer.Option(None, help="config.yaml 경로 (기본: src/pipeline/config.yaml)"),
    model_output: Optional[str] = typer.Option(None, help="모델 저장 경로 (config override)"),
    test_set_output: Optional[str] = typer.Option(
        None, help="Held-out test set 저장 경로 (config override, 병렬 실험 시 실험별로 분리 필요)"
    ),
    feature_columns_output: Optional[str] = typer.Option(None, help="Feature 목록 저장 경로 (config override)"),
    categorical_columns_output: Optional[str] = typer.Option(None, help="Categorical 카테고리 저장 경로 (config override)"),
    test_size: Optional[float] = typer.Option(None, help="Test set 비율 (config override)"),
    val_size: Optional[float] = typer.Option(None, help="Val set 비율 (config override)"),
    random_state: Optional[int] = typer.Option(None, help="Random state (config override, 데이터 split과 모델 둘 다 적용)"),
) -> None:
    """전체 파이프라인 실행: build-features -> train-model -> evaluate-model."""
    typer.echo("=" * 70)
    typer.echo("전체 파이프라인 실행")
    typer.echo("=" * 70)

    typer.echo("\n[1/3] build-features 실행...")
    build_training_dataset.main(
        raw_dir=raw_dir,
        events_path=events_path,
        output_path=dataset_path,
        videos_source=videos_source,
        personas_path=personas_path,
        events_source=events_source,
        events_start_date=events_start_date,
        events_end_date=events_end_date,
        topic_similarity_source=topic_similarity_source,
    )

    # 어떤 소스·기간의 데이터로 학습했는지 MLflow run에 항상 남긴다 — 기본값을
    # 썼는지 명시값을 썼는지와 무관하게 나중에 조회 가능해야 한다.
    data_source_params = {
        "videos_source": videos_source,
        "events_source": events_source,
        "topic_similarity_source": topic_similarity_source,
    }
    if events_source == "bigquery":
        data_source_params["events_start_date"] = events_start_date
        data_source_params["events_end_date"] = events_end_date

    typer.echo("\n[2/3] train-model 실행...")
    # train.main은 실현 sampling_rate(#300)를 반환한다 — evaluate가 오프라인
    # 지표(LogLoss/calibration)를 원분포 기준으로 재도록 그대로 넘긴다.
    realized_sampling_rate = train.main(
        config_path=config_path,
        data_path=dataset_path,
        model_output=model_output,
        test_set_output=test_set_output,
        feature_columns_output=feature_columns_output,
        categorical_columns_output=categorical_columns_output,
        test_size=test_size,
        val_size=val_size,
        random_state=random_state,
        extra_params=data_source_params,
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
        sampling_rate=realized_sampling_rate if realized_sampling_rate is not None else 1.0,
    )

    typer.echo("\n" + "=" * 70)
    typer.echo("파이프라인 완료")
    typer.echo("=" * 70)


@app.command()
def promote_model(
    model_name: str = typer.Option("ctr-model", help="Registry에 등록된 main 모델 이름"),
    champion_alias: str = typer.Option("champion", help="승격 대상 alias"),
    calibration_model_name: str = typer.Option(
        "ctr-calibration-model", help="짝 calibration 모델 이름(downsampling 후보용)"
    ),
) -> None:
    """게이트(지표 비교 + downsampling 페어링) 통과 시 신규 후보를 champion으로 승격."""
    try:
        promoted_version = promote.main(
            model_name=model_name,
            champion_alias=champion_alias,
            calibration_model_name=calibration_model_name,
        )
    except promote.GateRejectedError as exc:
        typer.echo(f"[게이트 미달] {exc}", err=True)
        raise typer.Exit(code=1)
    except Exception as exc:
        typer.echo(f"[에러] promote-model 실행 중 오류: {exc}", err=True)
        raise typer.Exit(code=1)

    if promoted_version is None:
        typer.echo(f"{model_name}: 평가할 신규 후보 버전 없음 — no-op")
    else:
        typer.echo(f"[OK] {model_name} v{promoted_version} -> @{champion_alias} 승격 완료")


if __name__ == "__main__":
    app()
