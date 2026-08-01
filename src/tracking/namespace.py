"""실험/운영 tracking·registry 네임스페이스 해석 (#406).

[파이프라인] 학습 구간의 MLflow 배선을 담당한다 — 어떤 tracking 서버에 붙고, 어떤
experiment에 run을 남기고, registered model을 어떤 이름으로 등록할지를 **한 곳에서**
결정한다.

[왜 필요한가] #396에서 로컬 에이전트가 `MLFLOW_TRACKING_URI=file:./mlruns`로 우회해
실험을 돌렸더니, 등록 이름이 config의 `registry.model_name`(=`ctr-model`)을 그대로 써서
**prod와 이름이 같은** 모델 버전이 로컬 스토어에 쌓였다. 이름이 같으면 승격 게이트가
보는 대상과 구분되지 않고, 트래킹 URI를 잘못 지정하면 실험 run이 그대로 prod
네임스페이스에 들어간다.

[기능] `resolve_tracking_namespace()`가 운영/실험 두 경로를 갈라 세 값(tracking URI,
experiment 이름, registry 모델 이름)을 함께 정한다. 세 값을 따로 만지면 하나만 빠뜨려
오염되므로 묶어서 돌려준다. 실험의 로컬 스토어 기본값은 프로젝트 루트 기준 **절대
경로**다 — CWD 상대면 실행 위치에 따라 실험 이력이 갈린다(#444).

[비책임] MLflow 호출 자체(`set_tracking_uri`, `get_or_create_experiment`)는
`src/tracking/client.py`가, 승격 판정은 `src/tracking/promote.py`가 소유한다.
"""

from __future__ import annotations

import os
import re
from collections.abc import Sequence
from dataclasses import dataclass
from typing import Optional

# 운영 학습 run이 쌓이는 experiment. 실험은 여기에 섞이지 않는다.
PROD_EXPERIMENT_NAME = "ctr-model-training"
# 실험 run 전용 experiment.
EXPERIMENT_EXPERIMENT_NAME = "ctr-model-experiment"
# 실험에서 트래킹 URI가 없을 때의 기본 경로. 로컬 파일 스토어라 서버 없이 돌아간다.
#
# **프로젝트 루트 기준 절대 경로**로 고정한다(#444). 예전에는 `file:./mlruns`라 CWD
# 상대였는데, 저장소 루트에서 돌리면 `<repo>/mlruns`, 하위 디렉토리에서 돌리면
# `<repo>/<subdir>/mlruns`로 갈렸다. 에이전트가 여러 라운드를 이어 돌릴 때 앞 라운드
# 이력을 못 찾는다.
_PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
EXPERIMENT_TRACKING_URI_DEFAULT = "file:" + os.path.join(_PROJECT_ROOT, "mlruns")
# registry 이름의 실험 네임스페이스 구분자.
EXPERIMENT_MODEL_NAME_INFIX = "-exp-"

_SLUG_SEPARATORS = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True, slots=True)
class TrackingNamespace:
    """한 학습 실행의 MLflow 좌표.

    Attributes:
        tracking_uri: MLflow tracking URI.
        experiment_name: run이 쌓일 experiment 이름.
        registry_model_name: registered model 이름.
        is_experiment: 실험 경로면 True.
    """

    tracking_uri: str
    experiment_name: str
    registry_model_name: str
    is_experiment: bool


def _slugify(value: str) -> str:
    return _SLUG_SEPARATORS.sub("-", value.strip().lower()).strip("-")


def derive_experiment_name(
    experiment: Optional[str], extra_features: Optional[Sequence[str]]
) -> Optional[str]:
    """실험 좌표를 쓸지, 쓴다면 어떤 이름으로 쓸지 정한다(#406 리뷰 1).

    `--extra-features`만 주고 `--experiment`를 빼면 prod 계약에 없는 입력으로 학습한
    산출물이 **prod registry 이름 아래** 쌓인다. 이 이슈가 없애려던 상황이 그대로
    재현되므로, 실험 피처가 있으면 실험 좌표를 암시한다.

    Args:
        experiment: 명시한 실험 이름(없으면 None).
        extra_features: 실험 피처 목록(없으면 None).

    Returns:
        실험 이름. 운영 경로면 None.
    """
    if experiment is not None:
        return experiment
    if extra_features:
        # 이름을 안 줬어도 어떤 피처를 얹은 실험인지는 이름에 남는다.
        return "-".join(extra_features)
    return None


def resolve_tracking_namespace(
    *,
    prod_model_name: str,
    experiment: Optional[str],
    tracking_uri_env: Optional[str],
) -> TrackingNamespace:
    """운영/실험 경로에 맞는 MLflow 좌표를 정한다(#406).

    Args:
        prod_model_name: config의 `registry.model_name`(운영 모델 이름).
        experiment: 실험 이름. None이면 운영 경로다.
        tracking_uri_env: `MLFLOW_TRACKING_URI` 환경변수 값(없으면 None).

    Returns:
        tracking URI · experiment 이름 · registry 모델 이름을 묶은 좌표.

    Raises:
        ValueError: 운영 경로인데 tracking URI가 없거나, 실험 이름이 쓸 수 있는
            slug를 만들지 못하면.
    """
    if EXPERIMENT_MODEL_NAME_INFIX in prod_model_name:
        # 운영 모델 이름에 실험 구분자가 들어가면 is_experiment_model_name()이
        # 운영 승격까지 조용히 막는다(#406 리뷰 4).
        raise ValueError(
            f"운영 모델 이름 {prod_model_name!r}에 실험 구분자"
            f"({EXPERIMENT_MODEL_NAME_INFIX!r})가 들어 있습니다. "
            "승격 게이트가 이 이름을 실험 모델로 오인해 운영 승격을 막습니다 — "
            "config의 registry.model_name을 다른 이름으로 바꾸십시오."
        )

    configured_uri = (tracking_uri_env or "").strip()

    if experiment is None:
        if not configured_uri:
            raise ValueError(
                "MLFLOW_TRACKING_URI가 설정되지 않았습니다. "
                "예전에는 http://localhost:5000으로 조용히 넘어가 연결 오류로 죽었는데, "
                "원인이 드러나지 않아 여기서 먼저 막습니다.\n"
                "운영 학습이면 tracking 서버 주소를 지정하고, 실험이면 "
                "--experiment <이름>을 주십시오 — 실험은 로컬 파일 스토어"
                f"({EXPERIMENT_TRACKING_URI_DEFAULT})를 기본값으로 씁니다."
            )
        return TrackingNamespace(
            tracking_uri=configured_uri,
            experiment_name=PROD_EXPERIMENT_NAME,
            registry_model_name=prod_model_name,
            is_experiment=False,
        )

    slug = _slugify(experiment)
    if not slug:
        raise ValueError(
            f"실험 이름 {experiment!r}에서 쓸 수 있는 식별자를 만들 수 없습니다. "
            "registry 이름에 들어가야 해서 ASCII 영숫자만 남기므로, 한글만으로 된 "
            "이름은 쓸 수 없습니다. 영문자·숫자를 최소 하나 포함해 주십시오"
            "(예: views_per_day)."
        )

    return TrackingNamespace(
        # URI를 명시했으면 존중한다 — 실험을 공용 서버에 남기고 싶을 수 있다.
        # 그 경우에도 experiment·registry 이름은 계속 분리된다.
        tracking_uri=configured_uri or EXPERIMENT_TRACKING_URI_DEFAULT,
        experiment_name=EXPERIMENT_EXPERIMENT_NAME,
        registry_model_name=f"{prod_model_name}{EXPERIMENT_MODEL_NAME_INFIX}{slug}",
        is_experiment=True,
    )


def is_experiment_model_name(model_name: str) -> bool:
    """registry 모델 이름이 실험 네임스페이스인지 판별한다(#406).

    승격 게이트가 실험 모델을 prod champion 후보로 착각하지 않게 하는 데 쓴다.
    """
    return EXPERIMENT_MODEL_NAME_INFIX in model_name
