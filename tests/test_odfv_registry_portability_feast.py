"""ODFV dill 페이로드가 apply 실행 위치에 의존하지 않는지 검증하는 게이트 (#409).

[무엇을 막는가] ``feast apply``는 정의 파일을 **cwd 기준 상대 경로**로 모듈명을 지어
import한다(``feast.repo_operations.py_path_to_module``). 배포 apply job은
``cd /app/feature_repo``에서 돌아 모듈명이 bare ``feature_definitions``로 잡히고, ODFV의
UDF를 dill로 절일 때 그 UDF가 참조하는 **같은 모듈의 전역 함수**가 그 이름으로
by-reference 기록된다. 반면 소비자(학습·서빙)는 ``/app``에서 돌아 그 이름을 import할 수
없어 레지스트리를 읽는 순간 ``ModuleNotFoundError: No module named 'feature_definitions'``로
죽는다. 즉 **레지스트리에 저장 시점의 cwd가 박히는** 부류의 결함이다.

[왜 이 형태인가] prod 레지스트리를 읽어야만 드러나는 것처럼 보이지만, 결정 요인은
레지스트리가 아니라 **직렬화 시점의 모듈명**이다. 그래서 이 테스트는 apply와 같은
모듈명(bare ``feature_definitions``)으로 정의를 로드해 UDF를 직렬화한 뒤, 소비자와 같은
import 경로(repo 루트만)에서 역직렬화한다 — GCS 레지스트리도 GKE도 없이 CI에서 재현된다.

[불변식] ODFV UDF가 참조하는 이름은 전부 ``feature_repo`` **바깥의 import 가능한
모듈**(``autoresearch.feature_engineering.*`` 등)에 있어야 한다. 정의 파일 안에 헬퍼를 두고 UDF에서 부르면
이 게이트가 실패한다. 데코레이터가 없는 한 아무리 얇은 래퍼라도 dill이 by-reference로
기록하므로, "테스트 용이성을 위한 분리"는 파일 **안**이 아니라 **밖**으로 해야 한다.

[보장 범위] 이 게이트가 보장하는 것은 **"repo 루트에서 import 가능한 이름만 참조한다"**
까지다. 역직렬화 직전에 이 저장소 소유 모듈을 ``sys.modules``에서 비우므로 "테스트
프로세스에 이미 로드돼 있어서 통과"하는 일은 없지만, 그 경로가 **학습 이미지의 코드
아카이브에 실제로 포함되는지**(예: UDF가 ``tests.*``를 참조하는 경우)는 여기서 볼 수 없다.
그 층까지 보려면 repo 루트만 PYTHONPATH로 준 별도 프로세스에서 역직렬화해야 한다.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath
from types import ModuleType

import pytest

pytest.importorskip("feast")

import dill  # noqa: E402
import pandas as pd  # noqa: E402
from feast.on_demand_feature_view import OnDemandFeatureView  # noqa: E402
from feast.repo_operations import py_path_to_module  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FEATURE_REPO = REPO_ROOT / "feature_repo"
DEFINITIONS = FEATURE_REPO / "feature_definitions.py"
# deployment/feast/apply-job.yaml이 `cd /app/feature_repo` 후 feast apply를 실행하므로
# 정의 파일의 모듈명은 패키지 경로가 아니라 bare 이름이 된다.
APPLY_MODULE_NAME = "feature_definitions"
# 이 저장소가 소유한 최상위 패키지. 역직렬화 직전에 sys.modules에서 비워, 이미 로드돼
# 있다는 이유로 통과하는 대신 소비자 경로에서 **실제로 import되는지**를 확인한다.
_PROJECT_TOP_LEVEL = frozenset({"autoresearch", "feature_repo"})


@pytest.fixture()
def apply_time_definitions(monkeypatch: pytest.MonkeyPatch) -> Iterator[ModuleType]:
    """배포 apply job과 **같은 모듈명**으로 feature_definitions를 로드한다.

    등록도 ``monkeypatch``에 맡긴다 — 아래 테스트가 같은 키를 ``delitem``하므로, 직접
    ``sys.modules``를 지우면 그 undo(재삽입)가 나중에 돌아 모듈이 세션에 남는다.
    """
    # 정의 파일이 import 시점에 요구하는 값(실제 값은 직렬화 결과와 무관).
    monkeypatch.setenv("GCP_PROJECT_ID", "odfv-portability-gate")
    monkeypatch.setenv("BQ_DATASET", "odfv-portability-gate")

    spec = importlib.util.spec_from_file_location(APPLY_MODULE_NAME, DEFINITIONS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, APPLY_MODULE_NAME, module)
    spec.loader.exec_module(module)
    yield module


def _udf_bodies(definitions: ModuleType) -> dict[str, bytes]:
    """정의 파일의 **모든** ODFV에서 레지스트리에 기록될 UDF 바이트를 뽑는다.

    이름을 하드코딩하지 않는다 — ODFV가 늘어나면 자동으로 이 게이트에 들어와야 한다.
    """
    odfvs = [v for v in vars(definitions).values() if isinstance(v, OnDemandFeatureView)]
    assert odfvs, "정의 파일에서 ODFV를 찾지 못했다 — 게이트가 무력화된 상태다"
    return {
        odfv.name: odfv.to_proto().spec.feature_transformation.user_defined_function.body
        for odfv in odfvs
    }


def _isolate_consumer_import_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """소비자(학습·서빙 파드)의 import 경로를 재현한다.

    ``feature_repo`` 디렉터리를 ``sys.path``에서 빼 apply 전용 bare 이름을 막고, 이 저장소
    소유 모듈을 ``sys.modules``에서 비워 **repo 루트 기준으로 다시 import되는지**까지 보게
    한다(테스트 프로세스에 이미 로드돼 있다는 이유로 통과하는 것을 막는다).
    """
    monkeypatch.setattr(
        sys, "path", [p for p in sys.path if Path(p or ".").resolve() != FEATURE_REPO]
    )
    for name in list(sys.modules):
        if name == APPLY_MODULE_NAME or name.split(".")[0] in _PROJECT_TOP_LEVEL:
            monkeypatch.delitem(sys.modules, name, raising=False)


def test_apply_job_module_name_matches_this_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """게이트가 가정한 apply 시점 모듈명이 배포 job·feast 규칙과 실제로 일치하는지 고정한다.

    아래 역직렬화 게이트는 "apply가 bare ``feature_definitions``로 절인다"는 전제 위에
    선다. 그 전제는 (a) apply job이 repo 디렉터리 안에서 돈다 (b) feast가 cwd 기준으로
    모듈명을 짓는다 — 둘 다 이 저장소 밖(배포 매니페스트·feast 버전)에서 바뀔 수 있으므로
    여기서 함께 고정한다. 전제가 바뀌면 게이트가 조용히 무력해지는 대신 이 테스트가 먼저 깨진다.
    """
    job = (REPO_ROOT / "deployment" / "feast" / "apply-job.yaml").read_text(encoding="utf-8")
    match = re.search(r"cd (\S+) && exec feast [^\n]*\bapply\b", job)
    assert match is not None, "apply-job.yaml에서 feast apply 실행 디렉터리를 찾지 못했다"
    # (a) apply의 cwd = feature_repo 디렉터리 자체(부모가 아니다).
    assert PurePosixPath(match.group(1)).name == FEATURE_REPO.name

    # (b) feast는 **cwd 기준** 상대 경로로 모듈명을 짓는다.
    monkeypatch.chdir(FEATURE_REPO)
    assert py_path_to_module(DEFINITIONS) == APPLY_MODULE_NAME


def test_every_odfv_udf_deserializes_in_consumer_import_path(
    apply_time_definitions: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply와 같은 모듈명으로 절인 UDF가 소비자 import 경로에서 전부 되살아나는지 (#409)."""
    # 레지스트리에 실제로 기록되는 바이트와 같은 경로로 뽑는다(apply가 쓰는 것과 동일).
    bodies = _udf_bodies(apply_time_definitions)
    _isolate_consumer_import_path(monkeypatch)

    for name, body in bodies.items():
        try:
            dill.loads(body)
        except (ModuleNotFoundError, AttributeError) as error:  # pragma: no cover - 진단용
            pytest.fail(
                f"ODFV '{name}'의 UDF가 소비자 경로에서 해소되지 않는 이름을 참조한다: "
                f"{type(error).__name__}: {error}. UDF가 부르는 헬퍼를 feature_repo 밖"
                "(autoresearch.feature_engineering.*)으로 옮겨라 (#409)."
            )


def test_category_match_udf_still_computes_after_round_trip(
    apply_time_definitions: ModuleType, monkeypatch: pytest.MonkeyPatch
) -> None:
    """되살린 UDF가 실제로 동작하는지까지 확인한다(이름만 맞고 몸통이 깨지는 경우 방지)."""
    body = _udf_bodies(apply_time_definitions)["category_match_view"]
    _isolate_consumer_import_path(monkeypatch)

    udf = dill.loads(body)
    result = udf(
        pd.DataFrame(
            {
                "preferred_category": ['["Gaming"]', '["Music"]'],
                "historical_category_affinity": ["Gaming", "unknown"],
                "category_id": ["Gaming", "Gaming"],
            }
        )
    )
    assert result["preferred_category_match"].tolist() == [1, 0]
    assert result["historical_category_match"].tolist() == [1, 0]
