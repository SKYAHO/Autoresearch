import ast
import textwrap

import pytest

from tools.archmap.module_info import extract_module_info
from tools.archmap.sidecar import (
    ArchSidecar,
    InvalidArchSidecarError,
    extract_arch_sidecar,
)

PATH = "autoresearch/action_logs/daily.py"


def _parse_sidecar(source: str) -> ArchSidecar | None:
    return extract_arch_sidecar(ast.parse(textwrap.dedent(source)), PATH, "action_logs")


def test_extracts_literal_arch_sidecar_into_module_info():
    # Given: 모듈 최상위에 네 필드를 가진 literal sidecar가 있다.
    source = textwrap.dedent(
        """
        __arch__ = {
            "stage": "action_logs",
            "role": "이벤트 로그를 조립합니다.",
            "owns": ["이벤트 확장", "checkpoint 저장"],
            "not_owns": ["CTR 학습셋 생성"],
        }
        """
    )

    # When: 모듈 정보를 추출한다.
    info = extract_module_info(
        source,
        "action_logs.daily",
        "action_logs",
        "autoresearch/action_logs/daily.py",
    )

    # Then: sidecar 설명 슬롯이 manifest 모듈 정보에 연결된다.
    assert info["role"] == "이벤트 로그를 조립합니다."
    assert info["owns"] == ["이벤트 확장", "checkpoint 저장"]
    assert info["not_owns"] == ["CTR 학습셋 생성"]


def test_preserves_empty_slots_when_sidecar_is_absent():
    # Given: __arch__가 없는 일반 모듈이다.
    source = "def run_daily(request):\n    return request\n"

    # When: 모듈 정보를 추출한다.
    info = extract_module_info(source, "action_logs.daily", "action_logs", PATH)

    # Then: 기존 부재 회귀값을 유지한다.
    assert info["role"] is None
    assert info["owns"] == []
    assert info["not_owns"] == []


def test_accepts_formatting_comments_and_preserves_list_order():
    # Given: 주석과 포맷이 달라도 목록의 authored order는 의미 데이터이다.
    source = """
    # sidecar comment
    __arch__={"stage": "action_logs",  # inline comment
              "role": "역할", "owns": ["두 번째", "첫 번째"],
              "not_owns": ["제외 두 번째", "제외 첫 번째"]}
    """

    # When: literal sidecar를 추출한다.
    sidecar = _parse_sidecar(source)

    # Then: 주석은 무시하고 목록 순서는 보존한다.
    assert sidecar == {
        "stage": "action_logs",
        "role": "역할",
        "owns": ["두 번째", "첫 번째"],
        "not_owns": ["제외 두 번째", "제외 첫 번째"],
    }


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "additional key",
            """
            __arch__ = {
                "stage": "action_logs", "role": "역할",
                "owns": [], "not_owns": [], "extra": "금지",
            }
            """,
        ),
        (
            "missing key",
            """
            __arch__ = {
                "stage": "action_logs", "role": "역할", "owns": [],
            }
            """,
        ),
        (
            "duplicate key",
            """
            __arch__ = {
                "stage": "action_logs", "stage": "action_logs",
                "role": "역할", "owns": [], "not_owns": [],
            }
            """,
        ),
    ],
)
def test_rejects_invalid_sidecar_keys(label: str, source: str) -> None:
    # Given: 계약 키가 추가·누락·중복된 sidecar이다.

    # When: literal sidecar를 추출한다.
    with pytest.raises(InvalidArchSidecarError) as error:
        _parse_sidecar(source)

    # Then: 경로를 포함한 전용 오류로 거부한다.
    assert type(error.value) is InvalidArchSidecarError
    assert error.value.path == PATH
    assert error.value.reason
    assert str(error.value) == f"{PATH}: {error.value.reason}"


def test_rejects_mixed_type_extra_keys_without_raw_type_error() -> None:
    # Given: 누락 키와 문자열·정수 추가 키가 함께 있어 추가 키를 정렬할 수 없다.
    source = """
    __arch__ = {
        "stage": "action_logs", "role": "역할", "owns": [],
        "extra": "금지", 1: "비교 불가",
    }
    """

    # When: malformed sidecar를 추출한다.
    with pytest.raises(InvalidArchSidecarError) as error:
        _parse_sidecar(source)

    # Then: 비교 불가능한 키도 경로가 보존된 전용 오류로 변환된다.
    assert error.value.path == PATH
    assert "__arch__ key" in error.value.reason
    assert "누락" in error.value.reason
    assert "추가" in error.value.reason


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "dynamic role",
            """
            role = "동적"
            __arch__ = {
                "stage": "action_logs", "role": role,
                "owns": [], "not_owns": [],
            }
            """,
        ),
        (
            "stage type",
            """
            __arch__ = {
                "stage": 1, "role": "역할", "owns": [], "not_owns": [],
            }
            """,
        ),
        (
            "role type",
            """
            __arch__ = {
                "stage": "action_logs", "role": ["역할"],
                "owns": [], "not_owns": [],
            }
            """,
        ),
        (
            "owns type",
            """
            __arch__ = {
                "stage": "action_logs", "role": "역할",
                "owns": "소유", "not_owns": [],
            }
            """,
        ),
        (
            "not_owns item type",
            """
            __arch__ = {
                "stage": "action_logs", "role": "역할",
                "owns": [], "not_owns": [1],
            }
            """,
        ),
        (
            "empty stage",
            """
            __arch__ = {
                "stage": "", "role": "역할", "owns": [], "not_owns": [],
            }
            """,
        ),
        (
            "empty role",
            """
            __arch__ = {
                "stage": "action_logs", "role": "", "owns": [], "not_owns": [],
            }
            """,
        ),
        (
            "empty list item",
            """
            __arch__ = {
                "stage": "action_logs", "role": "역할",
                "owns": [""], "not_owns": [],
            }
            """,
        ),
        (
            "duplicate owns item",
            """
            __arch__ = {
                "stage": "action_logs", "role": "역할",
                "owns": ["같음", "같음"], "not_owns": [],
            }
            """,
        ),
        (
            "duplicate not_owns item",
            """
            __arch__ = {
                "stage": "action_logs", "role": "역할",
                "owns": [], "not_owns": ["같음", "같음"],
            }
            """,
        ),
        (
            "stage mismatch",
            """
            __arch__ = {
                "stage": "training", "role": "역할", "owns": [], "not_owns": [],
            }
            """,
        ),
    ],
)
def test_rejects_invalid_sidecar_values(label: str, source: str) -> None:
    # Given: 값 타입·문자열·목록 중복·stage 계약을 위반한 sidecar이다.

    # When: literal sidecar를 추출한다.
    with pytest.raises(InvalidArchSidecarError) as error:
        _parse_sidecar(source)

    # Then: 전용 오류와 입력 경로를 반환한다.
    assert type(error.value) is InvalidArchSidecarError
    assert error.value.path == PATH
    assert error.value.reason
    assert str(error.value) == f"{PATH}: {error.value.reason}"


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "two declarations",
            """
            __arch__ = {"stage": "action_logs", "role": "첫째", "owns": [], "not_owns": []}
            __arch__ = {"stage": "action_logs", "role": "둘째", "owns": [], "not_owns": []}
            """,
        ),
        (
            "non-dict value",
            '__arch__ = ["action_logs", "역할", [], []]',
        ),
        (
            "dynamic dict value",
            """
            __arch__ = {
                "stage": "action_logs", "role": make_role(),
                "owns": [], "not_owns": [],
            }
            """,
        ),
    ],
)
def test_rejects_non_literal_or_repeated_declarations(label: str, source: str) -> None:
    # Given: sidecar 선언 수 또는 값이 AST literal 계약을 벗어난다.

    # When: literal sidecar를 추출한다.
    with pytest.raises(InvalidArchSidecarError) as error:
        _parse_sidecar(source)

    # Then: 실행하거나 임의의 선언을 선택하지 않고 전용 오류를 낸다.
    assert type(error.value) is InvalidArchSidecarError
    assert error.value.path == PATH
    assert error.value.reason


@pytest.mark.parametrize(
    ("label", "source"),
    [
        (
            "annotated assignment",
            '__arch__: dict = {"stage": "action_logs", "role": "역할", "owns": [], "not_owns": []}',
        ),
        (
            "augmented assignment",
            "__arch__ = {}\n__arch__ += {}",
        ),
        (
            "chained assignment",
            '__arch__ = alias = {"stage": "action_logs", "role": "역할", "owns": [], "not_owns": []}',
        ),
        (
            "named expression",
            'value = (__arch__ := {"stage": "action_logs", "role": "역할", "owns": [], "not_owns": []})',
        ),
        (
            "for target",
            "for __arch__ in []:\n    pass",
        ),
        (
            "comprehension target",
            "[item for __arch__ in []]",
        ),
        (
            "with-as target",
            "with context_manager() as __arch__:\n    pass",
        ),
        (
            "except-as target",
            "try:\n    pass\nexcept Exception as __arch__:\n    pass",
        ),
        (
            "import alias",
            "import package as __arch__",
        ),
        (
            "from import alias",
            "from package import name as __arch__",
        ),
        (
            "function name",
            "def __arch__():\n    pass",
        ),
        (
            "async function name",
            "async def __arch__():\n    pass",
        ),
        (
            "class name",
            "class __arch__:\n    pass",
        ),
        (
            "annotated type binding",
            "__arch__: type[int] = int",
        ),
        (
            "match capture",
            "match value:\n    case __arch__:\n        pass",
        ),
        (
            "control-flow assignment",
            '__arch__ = {"stage": "action_logs", "role": "역할", "owns": [], "not_owns": []}\nif enabled:\n    __arch__ = {}',
        ),
    ],
)
def test_rejects_forbidden_module_scope_arch_bindings(label: str, source: str) -> None:
    # Given: 허용된 단일 module Assign이 아닌 방식으로 __arch__를 bind한다.

    # When: AST sidecar parser를 호출한다.
    with pytest.raises(InvalidArchSidecarError) as error:
        _parse_sidecar(source)

    # Then: 모든 forbidden binding은 같은 전용 오류 경계로 거부한다.
    assert type(error.value) is InvalidArchSidecarError
    assert error.value.path == PATH
    assert error.value.reason


def test_ignores_same_name_inside_function_and_class_scopes():
    # Given: 함수·클래스 내부에서만 __arch__라는 이름을 사용한다.
    source = """
    def build():
        __arch__ = {"stage": "training", "role": "함수", "owns": [], "not_owns": []}

    class Holder:
        __arch__ = {"stage": "training", "role": "클래스", "owns": [], "not_owns": []}
    """

    # When: AST sidecar parser를 호출한다.
    sidecar = _parse_sidecar(source)

    # Then: module sidecar가 아니므로 부재로 처리한다.
    assert sidecar is None


def test_sidecar_facts_distinguish_missing_and_meaningful_change():
    # Given: 같은 public 변경을 설명하는 sidecar의 missing·reordered·meaningful 형태다.
    missing = extract_module_info(
        "def run(request):\n    return request\n",
        "action_logs.daily",
        "action_logs",
        PATH,
    )
    reordered = extract_module_info(
        '__arch__ = {"stage": "action_logs", "role": "역할",\n'
        '            "owns": ["둘", "하나"], "not_owns": ["셋"]}\n'
        'def run(request, optional=None):\n'
        '    return request\n',
        "action_logs.daily",
        "action_logs",
        PATH,
    )
    meaningful = extract_module_info(
        '__arch__ = {"stage": "action_logs", "role": "새 역할",\n'
        '            "owns": ["하나"], "not_owns": ["셋"]}\n'
        'def run(request, optional=None):\n'
        '    return request\n',
        "action_logs.daily",
        "action_logs",
        PATH,
    )

    # When: manifest sidecar facts를 비교한다.
    # Then: missing은 None으로 남고, 목록 순서만 다른 값과 의미 변경은 구분된다.
    assert missing["role"] is None
    assert reordered["role"] == "역할"
    assert set(reordered["owns"]) == {"하나", "둘"}
    assert meaningful["role"] != reordered["role"]
