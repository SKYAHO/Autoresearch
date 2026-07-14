"""MLflow 실험 기록 로거.

Parameter, Metric, Tag, Artifact를 MLflow에 기록합니다.
"""

from typing import Any, Dict, Optional

import mlflow


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


def log_artifact(artifact_path: str, artifact_type: str = "model") -> None:
    """Artifact 파일 기록.

    Args:
        artifact_path: 파일 경로
        artifact_type: Artifact 종류 (model, feature_list, config 등)
    """
    mlflow.log_artifact(artifact_path, artifact_path=artifact_type)


def log_artifacts(artifact_dir: str) -> None:
    """Artifact 디렉토리 기록.

    Args:
        artifact_dir: 디렉토리 경로
    """
    mlflow.log_artifacts(artifact_dir)
