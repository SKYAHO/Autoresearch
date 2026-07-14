"""MLflow Tracking Server 클라이언트.

Tracking URI 설정, Experiment 생성/조회, MLflow Client 초기화를 담당합니다.
"""

import os
from typing import Optional

import mlflow


def set_tracking_uri(uri: Optional[str] = None) -> None:
    """MLflow Tracking URI 설정.

    Args:
        uri: Tracking Server URI (예: http://localhost:5000, gs://bucket/mlflow)
             None이면 로컬 backend store 사용
    """
    if uri is None:
        uri = os.getenv("MLFLOW_TRACKING_URI", "file:./mlruns")
    mlflow.set_tracking_uri(uri)


def get_or_create_experiment(experiment_name: str) -> str:
    """Experiment 생성 또는 조회.

    Args:
        experiment_name: Experiment 이름

    Returns:
        Experiment ID
    """
    experiment = mlflow.get_experiment_by_name(experiment_name)
    if experiment is None:
        experiment_id = mlflow.create_experiment(experiment_name)
        return experiment_id
    return experiment.experiment_id


def set_experiment(experiment_name: str) -> None:
    """MLflow Experiment 설정.

    Args:
        experiment_name: Experiment 이름
    """
    experiment_id = get_or_create_experiment(experiment_name)
    mlflow.set_experiment(experiment_id=experiment_id)
