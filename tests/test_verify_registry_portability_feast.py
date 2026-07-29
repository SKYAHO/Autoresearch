"""apply 후 레지스트리 검증 스크립트가 stale payload를 실제로 잡는지 확인 (#409).

[무엇을 지키는가] ``scripts/verify_registry_portability.py``는 feast-apply 워크플로의
**마지막 방어선**이다. 이 스크립트가 조용히 통과하면 "apply 성공 → 레지스트리 stale →
학습만 죽음"이 그대로 프로덕션까지 간다(실제 #409에서 일어난 일).

그래서 두 방향을 다 고정한다: apply 시점 모듈명에 묶인 payload는 **반드시 실패**시키고,
소비자 경로에서 해소되는 payload는 **통과**시킨다. 레지스트리는 GCS를 쓰지 않고 이 자리에서
두 종류를 만들어 쓴다 — 판정을 가르는 건 저장 위치가 아니라 직렬화 시점의 모듈명이다.
"""

from __future__ import annotations

import importlib.util
import sys
from pathlib import Path
from types import ModuleType

import pytest

pytest.importorskip("feast")

from feast.protos.feast.core.Registry_pb2 import Registry as RegistryProto  # noqa: E402

REPO_ROOT = Path(__file__).resolve().parents[1]
FEATURE_REPO = REPO_ROOT / "feature_repo"
DEFINITIONS = FEATURE_REPO / "feature_definitions.py"
APPLY_MODULE_NAME = "feature_definitions"

sys.path.insert(0, str(REPO_ROOT / "scripts"))
import verify_registry_portability as verifier  # noqa: E402


def _load_as_apply_does(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    """apply job과 같은 bare 모듈명으로 정의를 로드한다(게이트 테스트와 같은 방식)."""
    monkeypatch.setenv("GCP_PROJECT_ID", "registry-verify-gate")
    monkeypatch.setenv("BQ_DATASET", "registry-verify-gate")
    spec = importlib.util.spec_from_file_location(APPLY_MODULE_NAME, DEFINITIONS)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, APPLY_MODULE_NAME, module)
    spec.loader.exec_module(module)
    return module


def _write_registry(path: Path, definitions: ModuleType) -> None:
    registry = RegistryProto()
    registry.on_demand_feature_views.append(definitions.category_match_view.to_proto())
    path.write_bytes(registry.SerializeToString())


# #409 이전 형태 재현 — 헬퍼가 **정의 파일 안**에 있던 시절. 별도 파일로 두는 이유는 두 가지다:
# ① 중첩 함수로 만들면 __qualname__이 `<locals>` 형태라 dill이 by-value로 절여 재현이 안 된다.
# ② feast 데코레이터가 udf_string을 소스에서 읽으므로 실제 .py 파일이어야 한다.
_STALE_MODULE_NAME = "stale_feature_definitions"
_STALE_SOURCE = '''
from feast import Field
from feast.on_demand_feature_view import on_demand_feature_view
from feast.types import Int64

from src.features.feature_builder import compute_category_matches


def local_matches(inputs):
    return compute_category_matches(inputs)


@on_demand_feature_view(
    sources=SOURCES,
    schema=[
        Field(name="preferred_category_match", dtype=Int64),
        Field(name="historical_category_match", dtype=Int64),
    ],
)
def stale_match_view(inputs):
    return local_matches(inputs)
'''


def _make_stale_registry(path: Path, definitions: ModuleType, workdir: Path) -> None:
    """UDF가 **자기 모듈의 헬퍼**를 부르는 ODFV를 직렬화해 stale 레지스트리를 만든다.

    그 모듈은 어디서도 import할 수 없는 이름이라, dill이 by-reference로 기록한 순간
    소비자 경로에서 해소되지 않는다 — prod에서 실제로 벌어진 일과 같은 구조다.
    """
    source_path = workdir / f"{_STALE_MODULE_NAME}.py"
    source_path.write_text(_STALE_SOURCE, encoding="utf-8")

    spec = importlib.util.spec_from_file_location(_STALE_MODULE_NAME, source_path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    # 소스 뷰는 실제 정의에서 빌려 온다(ODFV 생성에 필요할 뿐 판정과 무관).
    module.SOURCES = [
        definitions.user_static_view,
        definitions.user_dynamic_view,
        definitions.video_feature_view,
    ]
    sys.modules[_STALE_MODULE_NAME] = module
    try:
        spec.loader.exec_module(module)
        registry = RegistryProto()
        registry.on_demand_feature_views.append(module.stale_match_view.to_proto())
        path.write_bytes(registry.SerializeToString())
    finally:
        sys.modules.pop(_STALE_MODULE_NAME, None)


@pytest.fixture(autouse=True)
def consumer_import_path(monkeypatch: pytest.MonkeyPatch) -> None:
    """스크립트가 sys.path를 제자리에서 바꾸므로 테스트 뒤 원복을 보장한다."""
    monkeypatch.setattr(sys, "path", list(sys.path))


def test_passes_on_a_registry_written_by_current_definitions(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definitions = _load_as_apply_does(monkeypatch)
    registry_path = tmp_path / "registry.db"
    _write_registry(registry_path, definitions)
    monkeypatch.delitem(sys.modules, APPLY_MODULE_NAME)

    assert verifier.main([str(registry_path)]) == 0


def test_fails_on_a_stale_registry_bound_to_the_apply_cwd(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    definitions = _load_as_apply_does(monkeypatch)
    registry_path = tmp_path / "registry.db"
    _make_stale_registry(registry_path, definitions, tmp_path)
    monkeypatch.delitem(sys.modules, APPLY_MODULE_NAME)

    # 이게 통과하면 배포 검증이 무력해진 것 — #409가 그대로 프로덕션까지 간다.
    assert verifier.main([str(registry_path)]) == 1


def test_fails_when_the_registry_has_no_odfv(tmp_path: Path) -> None:
    """0건을 "검증할 게 없으니 통과"로 두면 잘못 받은 레지스트리가 조용히 지나간다."""
    registry_path = tmp_path / "empty.db"
    registry_path.write_bytes(RegistryProto().SerializeToString())

    assert verifier.main([str(registry_path)]) == 1
