"""MLflow 실험 기록 로거.

Parameter, Metric, Tag, Artifact를 MLflow에 기록합니다.
"""

from typing import Any, Dict, Optional

import mlflow
import mlflow.data
import pandas as pd


def log_parameters(params: Dict[str, Any]) -> None:
    """Parameter 기록.

    Args:
        params: 파라미터 딕셔너리 (예: {"learning_rate": 0.01, "n_estimators": 100})
    """
    mlflow.log_params(params)


def log_dataset(
    df: pd.DataFrame,
    *,
    name: str,
    source: str,
    context: str = "training",
    targets: Optional[str] = None,
    tags: Optional[Dict[str, str]] = None,
) -> None:
    """DataFrame을 MLflow run의 dataset input(lineage)으로 기록한다.

    log_parameters(파라미터)와 달리 run의 **Datasets** 섹션에 dataset 엔티티(이름·
    source·스키마·행 수)를 남겨, 어떤 데이터로 학습했는지를 파라미터가 아니라
    lineage로 추적할 수 있게 한다(#359). 활성 run 컨텍스트 안에서 호출해야 한다
    (다른 log_* 래퍼와 동일하게 run은 호출부가 연다).

    Args:
        df: 기록할 데이터셋(스키마·행 수가 프로파일로 요약된다).
        name: dataset 이름(예: "training_dataset").
        source: 데이터 출처 URI/경로(예: training_dataset.csv 경로).
        context: 이 run에서 데이터셋의 용도(예: "training").
        targets: 라벨 컬럼명(있으면 dataset이 supervised로 기록된다).
        tags: dataset input에 붙일 provenance 태그(피처 소스·기간·행 수 등).
    """
    dataset = mlflow.data.from_pandas(df, source=source, name=name, targets=targets)
    mlflow.log_input(dataset, context=context, tags=tags)


def log_metrics(metrics: Dict[str, float], step: Optional[int] = None) -> None:
    """Metric 기록.

    Args:
        metrics: 지표 딕셔너리 (예: {"roc_auc": 0.85, "pr_auc": 0.82})
        step: Step 번호 (optional)
    """
    for key, value in metrics.items():
        mlflow.log_metric(key, value, step=step)


def log_tags(tags: Dict[str, str]) -> None:
    """Tag 기록 (실험 식별 정보).

    Args:
        tags: 태그 딕셔너리 (예: {"git_sha": "abc123", "dataset_id": "ds_001"})
    """
    mlflow.set_tags(tags)


def log_artifact(
    local_path: Optional[str] = None,
    artifact_path: str = "model",
    artifact_type: Optional[str] = None,
) -> None:
    """Artifact 파일 기록.

    Args:
        local_path: 로컬 파일 경로
        artifact_path: MLflow Artifact 저장 경로
        artifact_type: 이전 호출부 호환용 Artifact 종류
    """
    if local_path is None:
        raise ValueError("local_path is required")
    if artifact_type is not None:
        artifact_path = artifact_type
    mlflow.log_artifact(local_path, artifact_path=artifact_path)


def log_artifacts(artifact_dir: str) -> None:
    """Artifact 디렉토리 기록.

    Args:
        artifact_dir: 디렉토리 경로
    """
    mlflow.log_artifacts(artifact_dir)


def start_run(run_name: Optional[str] = None, tags: Optional[Dict[str, str]] = None) -> str:
    """MLflow Run 시작.

    Args:
        run_name: Run 이름
        tags: 초기 태그

    Returns:
        Run ID
    """
    run = mlflow.start_run(run_name=run_name, tags=tags)
    return run.info.run_id


def end_run() -> None:
    """MLflow Run 종료."""
    mlflow.end_run()


def log_onnx_model(onnx_model: Any, artifact_path: str = "model_onnx") -> None:
    """ONNX로 변환된 모델을 MLflow에 기록한다(mlflow.onnx.log_model 래퍼, #302/#179).

    Args:
        onnx_model: onnx.ModelProto (예: src.utils.model_utils.convert_lgbm_to_onnx 반환값).
        artifact_path: MLflow artifact 저장 경로(서빙 로더의 ONNX 경로 상수와 계약).
    """
    import mlflow.onnx

    mlflow.onnx.log_model(onnx_model, artifact_path=artifact_path)
