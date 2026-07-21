"""MLflow 실험 기록 로거.

Parameter, Metric, Tag, Artifact를 MLflow에 기록합니다.
"""

from typing import Any, Dict, Optional

import mlflow

__arch__ = {
    "stage": "training",
    "role": "MLflow run에 파라미터·지표·태그·아티팩트를 기록합니다.",
    "owns": [
        "파라미터/지표/태그 기록",
        "로컬 파일·디렉토리 아티팩트 기록",
        "ONNX 모델 아티팩트 기록",
        "run 시작/종료",
    ],
    "not_owns": ["모델 학습", "Model Registry 등록"],
}


def log_parameters(params: Dict[str, Any]) -> None:
    """Parameter 기록.

    Args:
        params: 파라미터 딕셔너리 (예: {"learning_rate": 0.01, "n_estimators": 100})
    """
    mlflow.log_params(params)


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


def log_onnx_model(onnx_model: Any, artifact_path: str = "model_onnx") -> None:
    """ONNX로 변환된 모델을 MLflow에 기록(mlflow.onnx.log_model 래퍼).

    Args:
        onnx_model: onnx.ModelProto (예: src.utils.model_utils.convert_lgbm_to_onnx
            반환값)
        artifact_path: MLflow artifact 저장 경로
    """
    import mlflow.onnx

    mlflow.onnx.log_model(onnx_model, artifact_path=artifact_path)


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
