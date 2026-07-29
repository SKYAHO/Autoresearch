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
모듈**(``src.features.*`` 등)에 있어야 한다. 정의 파일 안에 헬퍼를 두고 UDF에서 부르면
이 게이트가 실패한다.
"""

from __future__ import annotations

import importlib.util
import re
import sys
from collections.abc import Iterator
from pathlib import Path, PurePosixPath

import pytest

pytest.importorskip("feast")

import dill  # noqa: E402
import pandas as pd  # noqa: E402
from feast.repo_operations import py_path_to_module  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FEATURE_REPO = REPO_ROOT / "feature_repo"
DEFINITIONS = FEATURE_REPO / "feature_definitions.py"
# deploy/feast/apply-job.yaml이 `cd /app/feature_repo` 후 feast apply를 실행하므로
# 정의 파일의 모듈명은 패키지 경로가 아니라 bare 이름이 된다.
APPLY_MODULE_NAME = "feature_definitions"


@pytest.fixture()
def apply_time_definitions(monkeypatch: pytest.MonkeyPatch) -> Iterator[object]:
    """배포 apply job과 **같은 모듈명**으로 feature_definitions를 로드한다."""
    # 정의 파일이 import 시점에 요구하는 값(실제 값은 직렬화 결과와 무관).
    monkeypatch.setenv("GCP_PROJECT_ID", "odfv-portability-gate")
    monkeypatch.setenv("BQ_DATASET", "odfv-portability-gate")

    spec = importlib.util.spec_from_file_location(APPLY_MODULE_NAME, DEFINITIONS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[APPLY_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        yield module
    finally:
        sys.modules.pop(APPLY_MODULE_NAME, None)


def test_apply_job_module_name_matches_this_gate(monkeypatch: pytest.MonkeyPatch) -> None:
    """게이트가 가정한 apply 시점 모듈명이 배포 job·feast 규칙과 실제로 일치하는지 고정한다.

    아래 역직렬화 게이트는 "apply가 bare ``feature_definitions``로 절인다"는 전제 위에
    선다. 그 전제는 (a) apply job이 repo 디렉터리 안에서 돈다 (b) feast가 cwd 기준으로
    모듈명을 짓는다 — 둘 다 이 저장소 밖(배포 매니페스트·feast 버전)에서 바뀔 수 있으므로
    여기서 함께 고정한다. 전제가 바뀌면 게이트가 조용히 무력해지는 대신 이 테스트가 먼저 깨진다.
    """
    job = (REPO_ROOT / "deploy" / "feast" / "apply-job.yaml").read_text(encoding="utf-8")
    match = re.search(r"cd (\S+) && exec feast [^\n]*\bapply\b", job)
    assert match is not None, "apply-job.yaml에서 feast apply 실행 디렉터리를 찾지 못했다"
    # (a) apply의 cwd = feature_repo 디렉터리 자체(부모가 아니다).
    assert PurePosixPath(match.group(1)).name == FEATURE_REPO.name

    # (b) feast는 **cwd 기준** 상대 경로로 모듈명을 짓는다.
    monkeypatch.chdir(FEATURE_REPO)
    assert py_path_to_module(DEFINITIONS) == APPLY_MODULE_NAME


def test_odfv_udf_deserializes_without_feature_repo_on_sys_path(
    apply_time_definitions: object, monkeypatch: pytest.MonkeyPatch
) -> None:
    """apply와 같은 모듈명으로 절인 UDF가 소비자 import 경로에서 되살아나는지 (#409)."""
    odfv = apply_time_definitions.category_match_view  # type: ignore[attr-defined]
    # 레지스트리에 실제로 기록되는 바이트와 같은 경로로 뽑는다(apply가 쓰는 것과 동일).
    body = odfv.to_proto().spec.feature_transformation.user_defined_function.body

    # 소비자(학습·서빙)의 import 경로 재현: repo 루트만 보이고 feature_repo 디렉터리는
    # sys.path에 없으며, apply가 쓰던 bare 모듈명도 이미 사라진 상태다.
    monkeypatch.delitem(sys.modules, APPLY_MODULE_NAME)
    monkeypatch.setattr(
        sys, "path", [p for p in sys.path if Path(p or ".").resolve() != FEATURE_REPO]
    )

    try:
        udf = dill.loads(body)
    except ModuleNotFoundError as error:  # pragma: no cover - 실패 경로 진단용
        pytest.fail(
            f"ODFV UDF가 apply 실행 위치에 묶인 모듈을 참조한다: {error}. "
            "UDF가 부르는 헬퍼를 feature_repo 밖(src.features.*)으로 옮겨라 (#409)."
        )

    # 되살린 UDF가 실제로 동작하는지까지 확인한다(이름만 맞고 몸통이 깨지는 경우 방지).
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
