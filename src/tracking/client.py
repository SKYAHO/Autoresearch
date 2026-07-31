"""MLflow Tracking Server 클라이언트.

Tracking URI 설정, Experiment 생성/조회, MLflow Client 초기화를 담당합니다.
"""

from typing import Optional

import mlflow


def set_tracking_uri(uri: Optional[str] = None) -> None:
    """MLflow Tracking URI 설정.

    좌표를 정하는 책임은 `src/tracking/namespace.py`에 있다(#406). 이 함수는 이미
    결정된 URI를 MLflow에 넘기는 얇은 배관이며, 폴백 기본값을 스스로 정하지 않는다.

    예전에는 `uri=None`일 때 `file:./mlruns`(CWD 상대)로 떨어졌는데, 실행 위치에
    따라 실험 이력이 갈리는 문제가 있었다(#444). 이 함수는 공개 API라 새 호출부가
    인자를 생략하는 순간 그 형태가 되살아나므로, 아예 필수로 만들어 막는다.

    Args:
        uri: Tracking Server URI. 빈 값이면 거부한다 — 실험은
            `resolve_tracking_namespace()`가 정한 값을, 운영은 `MLFLOW_TRACKING_URI`를
            넘겨야 한다.

    Raises:
        ValueError: uri가 None이거나 빈 문자열이면.
    """
    resolved = (uri or "").strip()
    if not resolved:
        raise ValueError(
            "tracking URI가 비었습니다. 좌표는 호출부가 정해서 넘겨야 합니다 — "
            "실험은 resolve_tracking_namespace()의 결과를, 운영은 "
            "MLFLOW_TRACKING_URI 값을 넘기십시오(#444)."
        )
    mlflow.set_tracking_uri(resolved)


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
