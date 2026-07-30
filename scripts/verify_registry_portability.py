"""apply된 Feast 레지스트리를 소비자 import 경로에서 실제로 열어 보는 배포 후 검증 (#409).

[파이프라인] 피처 구간 배포 검증 — ``feast apply``가 **끝난 뒤** GCS 레지스트리를 받아,
학습·materialize 파드와 같은 import 경로에서 ODFV UDF를 역직렬화한다. 정의를 쓰는 쪽(apply
job)이 아니라 **읽는 쪽**의 성공 여부를 판정하는 구간이다.

[왜 필요한가] 이 부류는 apply가 성공해도 조용히 통과한다. 실제로 #409 수정(PR #410) 머지 후
apply는 성공했지만 레지스트리의 ODFV는 그대로였다 — ``registry.apply_feature_view``가
``_schema_or_udf_changed``가 False면 meta timestamp만 갱신하고 spec(=dill body)을 보존하는데,
ODFV의 그 판정은 ``PandasTransformation.__eq__`` = (``udf_string``, ``co_code``) 두 가지뿐이라
**UDF가 부르는 헬퍼의 소속 모듈이 바뀐 것을 못 본다**. 기존 침묵 실패 가드(registry generation
변경 여부)는 meta 갱신만으로도 통과하므로 이 층을 대신하지 못한다.

[무엇을 보장하는가]
  ① 모든 ODFV UDF가 소비자 import 경로에서 **역직렬화된다**.
  ② UDF가 by-reference로 참조하는 이름 중 이 저장소 소유 모듈은 **``src/`` 밑에만** 있다.
     ②는 ①로 안 잡히는 경우를 위한 것이다 — 소비자 파드의 ``/app``은 ``git archive``로 푼
     저장소 전체라 ``feature_repo.*``/``tests.*`` 참조도 "import는 되기" 때문에, import
     성공만 보면 통과해 버린다. 참조 위치를 직접 보고 좁힌다.

[보장하지 못하는 것] "레지스트리가 현재 코드와 **동등한가**"는 직접 보지 않는다 — apply가
갱신을 스킵해도 남은 옛 payload가 위 ①②를 만족하면 통과한다. 다만 그렇게 남을 수 있는 것은
많지 않다.

  - UDF 본체는 by-value로 절여지는데, 그게 달라지면 ``udf_string``/바이트코드가 달라져
    feast가 갱신을 스킵하지 않는다. 스키마(``features``)·mode·소스 투영·entity_columns도
    ``_schema_or_udf_changed``가 비교한다.
  - UDF가 부르는 **헬퍼는 by-reference**라 역직렬화 시점에 현재 코드에서 해소된다. 헬퍼의
    구현이 바뀌어도 레지스트리는 손댈 필요가 없고, 실제로 옛 payload를 읽어도 새 헬퍼가
    호출된다(실측). 즉 헬퍼 변경은 stale의 원인이 아니다.
  - 그래서 실질적으로 남는 stale 표면은 **by-reference 대상의 모듈 경로가 바뀐 경우**이고,
    그건 ①②가 잡는다. 그 밖에는 비교 대상에서 빠진 description/tags 정도다.

헬퍼 출력이 선언된 스키마와 어긋나는 부류(ODFV 계약 위반)는 이 스크립트가 아니라 CI의
``tests/test_odfv_category_match_feast.py``(실제 store로 조회해 컬럼을 대조)가 담당한다.

[전제] 소비자 파드의 ``/app``은 ``scripts/upload_code_archive.sh``의 ``git archive`` 결과다
(``.gitattributes``에 ``export-ignore`` 없음 = 추적 파일 전체). 그래서 이 스크립트를 저장소
루트에서 돌리는 것이 소비자 환경과 같다. 아카이브를 좁히는 변경이 생기면 이 전제가 깨진다.

[비책임] ODFV/FeatureView 정의는 ``feature_repo``가, apply 실행·삭제 스캔은
``deploy/feast/apply-job.yaml``과 ``.github/workflows/feast-apply.yml``이 소유한다.

사용법 (repo 루트에서 실행 — 소비자 파드의 cwd=/app에 대응):
  uv run --no-sync python scripts/verify_registry_portability.py registry.db
"""

from __future__ import annotations

import argparse
import io
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
FEATURE_REPO = REPO_ROOT / "feature_repo"
# UDF가 참조해도 되는 이 저장소 소유 코드의 위치. 학습·서빙·시뮬이 공유하는 변환 본체는
# 전부 여기 있고, 정의 파일(feature_repo)은 apply 실행 위치에 묶이므로 참조 대상이 될 수 없다.
ALLOWED_FIRST_PARTY = REPO_ROOT / "src"


def isolate_consumer_import_path() -> None:
    """소비자(학습·materialize 파드)와 같은 import 경로를 만든다.

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
    kept = [p for p in sys.path if Path(p or ".").resolve() not in (FEATURE_REPO, here)]
    if str(REPO_ROOT) not in kept:
        kept.insert(0, str(REPO_ROOT))
    sys.path[:] = kept


def _load_recording_references(body: bytes) -> set[tuple[str, str]]:
    """UDF를 역직렬화하면서 by-reference로 참조된 ``(모듈, 이름)``을 모두 기록한다.

    역직렬화 결과의 ``__globals__``로는 알 수 없다 — 거기 담기는 건 **해소된 객체**라
    ``__module__``이 그 객체의 현재 위치를 가리키지, 레지스트리가 적어 둔 이름이 아니다.
    unpickler의 ``find_class``가 그 이름을 그대로 받는 유일한 지점이다.
    """
    import dill

    references: set[tuple[str, str]] = set()

    class _RecordingUnpickler(dill.Unpickler):  # type: ignore[misc]
        def find_class(self, module: str, name: str):
            references.add((module, name))
            return super().find_class(module, name)

    _RecordingUnpickler(io.BytesIO(body)).load()
    return references


def _first_party_violation(module: str) -> str | None:
    """참조된 모듈이 이 저장소 소유이면서 ``src/`` 밖이면 그 파일 경로를 돌려준다."""
    resolved = sys.modules.get(module)
    origin = getattr(resolved, "__file__", None)
    if origin is None:
        return None  # builtins 등 파일 없는 모듈
    path = Path(origin).resolve()
    if not path.is_relative_to(REPO_ROOT):
        return None  # 저장소 밖 — 서드파티·표준 라이브러리
    relative = path.relative_to(REPO_ROOT)
    # 가상환경이 저장소 안(.venv)에 있으면 서드파티 패키지도 "저장소 안"으로 잡힌다.
    # 숨김 디렉터리와 site-packages는 이 저장소가 소유한 코드가 아니다.
    if "site-packages" in relative.parts or relative.parts[0].startswith("."):
        return None
    if path.is_relative_to(ALLOWED_FIRST_PARTY):
        return None
    return str(relative)


def check_udf_portability(
    registry_path: str | Path,
) -> tuple[list[str], list[str], list[str]]:
    """레지스트리의 모든 ODFV UDF를 역직렬화하고 참조 위치를 검사한다.

    Returns:
        ``(검사한 ODFV, UDF가 없어 건너뛴 ODFV, 실패 진단 문자열들)``.
    """
    from feast.protos.feast.core.Registry_pb2 import Registry as RegistryProto

    registry = RegistryProto()
    registry.ParseFromString(Path(registry_path).read_bytes())

    checked: list[str] = []
    skipped: list[str] = []
    failures: list[str] = []
    for odfv in registry.on_demand_feature_views:
        name = odfv.spec.name
        body = odfv.spec.feature_transformation.user_defined_function.body
        if not body:
            # UDF 없는 ODFV(집계 전용 등)는 검사 대상이 아니지만, 조용히 사라지면 "몇 건을
            # 봤는가"가 흐려지므로 별도로 셈해 출력한다.
            skipped.append(name)
            continue
        checked.append(name)
        try:
            references = _load_recording_references(body)
        except Exception as error:  # noqa: BLE001 - 어떤 해소 실패든 배포 실패로 다룬다
            failures.append(f"{name}: 역직렬화 실패 — {type(error).__name__}: {error}")
            continue
        for module, attribute in sorted(references):
            location = _first_party_violation(module)
            if location is not None:
                failures.append(
                    f"{name}: 저장소 안 {location}의 {module}.{attribute}를 참조 "
                    "(UDF는 src/ 밖 모듈을 참조하면 안 된다)"
                )
    return checked, skipped, failures


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="apply된 Feast 레지스트리 검증 (#409)")
    parser.add_argument("registry_path", help="apply 후 내려받은 registry.db 경로")
    args = parser.parse_args(argv)

    isolate_consumer_import_path()
    checked, skipped, failures = check_udf_portability(args.registry_path)
    if skipped:
        print(f"UDF가 없어 건너뛴 ODFV {len(skipped)}건: {', '.join(skipped)}")
    if not checked:
        # 0건을 "검증할 게 없으니 통과"로 두면 잘못 받은 레지스트리가 조용히 지나간다.
        # 이 저장소의 ctr_training_v1은 ODFV를 포함하므로 0건 자체가 이상 신호다.
        print("레지스트리에 UDF를 가진 ODFV가 하나도 없다 — 레지스트리 경로·apply 결과를 확인하라.")
        return 1
    if failures:
        print("레지스트리의 ODFV UDF가 소비자 경로에서 성립하지 않는다:")
        for failure in failures:
            print(f"  - {failure}")
        print(
            "\n확인할 것:\n"
            "  1) UDF가 부르는 헬퍼가 src/ 밑에 있는가 (feature_repo 안에 두면 apply 실행\n"
            "     위치에 묶인 이름으로 기록된다)\n"
            "  2) 헬퍼만 옮긴 변경이라면 feast가 '변경 없음'으로 판정해 옛 dill body를\n"
            "     보존했을 수 있다 — udf_string 또는 UDF 바이트코드가 바뀌어야 갱신된다 (#409)"
        )
        return 1

    print(
        f"레지스트리의 ODFV UDF {len(checked)}건이 소비자 경로에서 성립한다: {', '.join(checked)}"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
