"""apply된 Feast 레지스트리를 소비자 import 경로에서 실제로 열어 보는 배포 후 검증 (#409).

[파이프라인] 피처 구간 배포 검증 — ``feast apply``가 **끝난 뒤** GCS 레지스트리를 받아,
학습·서빙 파드와 같은 import 경로에서 ODFV UDF를 역직렬화한다. 정의를 쓰는 쪽(apply job)이
아니라 **읽는 쪽**의 성공 여부를 판정하는 구간이다.

[왜 필요한가] 이 부류는 apply가 성공해도 조용히 통과한다. 실제로 #409 수정(PR #410) 머지 후
apply는 성공했지만 레지스트리의 ODFV는 그대로였다 — ``registry.apply_feature_view``가
``_schema_or_udf_changed``가 False면 meta timestamp만 갱신하고 spec(=dill body)을 보존하는데,
ODFV의 그 판정은 ``PandasTransformation.__eq__`` = (``udf_string``, ``co_code``) 두 가지뿐이라
**UDF가 부르는 헬퍼의 소속 모듈이 바뀐 것을 못 본다**. 기존 침묵 실패 가드(registry generation
변경 여부)는 meta 갱신만으로도 통과하므로 이 층을 대신하지 못한다.

[게이트 3층] ① CI ``tests/test_odfv_registry_portability_feast.py`` = **코드가** 이식 가능한
payload를 만드는가(머지 전) ② 이 스크립트 = **apply가** 그 payload를 레지스트리에 실제로
넣었는가(배포 후) ③ 학습 DAG 완주 = 나머지 전 구간. ①만으로는 ②를 보증하지 못한다.

[비책임] ODFV/FeatureView 정의는 ``feature_repo``가, apply 실행·삭제 스캔은
``deploy/feast/apply-job.yaml``과 ``.github/workflows/feast-apply.yml``이 소유한다.

사용법 (repo 루트에서 실행 — 소비자 파드의 cwd=/app에 대응):
  uv run --no-sync python scripts/verify_registry_portability.py registry.db
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FEATURE_REPO = REPO_ROOT / "feature_repo"


class RegistryPortabilityError(RuntimeError):
    """레지스트리의 UDF가 소비자 import 경로에서 해소되지 않는다."""


def isolate_consumer_import_path() -> None:
    """소비자(학습·서빙 파드)와 같은 import 경로를 만든다.

    소비자는 ``/app``(=repo 루트)에서 ``python -m src.cli``로 돌아 **repo 루트만** import
    경로에 있다. 반면 apply job은 ``cd /app/feature_repo``에서 돌아 정의 모듈이 bare 이름으로
    잡힌다. 그래서 두 가지를 맞춘다.

    - ``feature_repo`` 디렉터리 제거: 있으면 apply 시점 bare 이름이 그대로 import돼 검증이
      무의미해진다.
    - 이 스크립트가 있는 ``scripts`` 디렉터리를 repo 루트로 교체: ``python scripts/x.py``로
      실행하면 파이썬이 ``scripts``를 ``sys.path[0]``에 넣어 ``src.*``가 안 보인다. 이건
      실행 방식의 부산물이지 소비자 환경이 아니다.
    """
    here = Path(__file__).resolve().parent
    kept = [
        p
        for p in sys.path
        if Path(p or ".").resolve() not in (FEATURE_REPO, here)
    ]
    if str(REPO_ROOT) not in kept:
        kept.insert(0, str(REPO_ROOT))
    sys.path[:] = kept


def check_udf_portability(registry_path: str | Path) -> tuple[list[str], list[str]]:
    """레지스트리의 모든 ODFV UDF를 역직렬화한다.

    Returns:
        ``(검사한 ODFV 이름들, 실패 진단 문자열들)``. UDF가 없는 ODFV는 건너뛴다.
    """
    import dill
    from feast.protos.feast.core.Registry_pb2 import Registry as RegistryProto

    registry = RegistryProto()
    registry.ParseFromString(Path(registry_path).read_bytes())

    checked: list[str] = []
    failures: list[str] = []
    for odfv in registry.on_demand_feature_views:
        body = odfv.spec.feature_transformation.user_defined_function.body
        if not body:
            continue
        checked.append(odfv.spec.name)
        try:
            dill.loads(body)
        except Exception as error:  # noqa: BLE001 - 어떤 해소 실패든 배포 실패로 다룬다
            failures.append(f"{odfv.spec.name}: {type(error).__name__}: {error}")
    return checked, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("registry_path", help="apply 후 내려받은 registry.db 경로")
    args = parser.parse_args(argv)

    isolate_consumer_import_path()
    checked, failures = check_udf_portability(args.registry_path)
    if not checked:
        # ODFV가 없으면 검증할 게 없다는 뜻인데, 이 저장소는 ctr_training_v1이 ODFV를 포함하므로
        # 0건은 레지스트리를 잘못 받았거나 apply가 정의를 통째로 날린 신호다 — 통과시키지 않는다.
        print("레지스트리에 UDF를 가진 ODFV가 하나도 없다 — 레지스트리 경로·apply 결과를 확인하라.")
        return 1
    if failures:
        print("레지스트리의 ODFV UDF가 소비자 import 경로에서 해소되지 않는다:")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\napply는 성공했지만 레지스트리 내용이 stale하거나, UDF가 apply 실행 위치에 묶인\n"
            "모듈을 참조한다. 확인할 것:\n"
            "  1) UDF가 부르는 헬퍼가 feature_repo 밖(src.features.*)에 있는가\n"
            "  2) 헬퍼만 옮긴 변경이라면 feast가 '변경 없음'으로 판정해 dill body를 보존했을 수\n"
            "     있다 — udf_string 또는 UDF 바이트코드가 바뀌어야 레지스트리가 갱신된다 (#409)"
        )
        return 1

    print(
        f"레지스트리의 ODFV UDF {len(checked)}건이 소비자 import 경로에서 해소된다: "
        f"{', '.join(checked)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
