from __future__ import annotations

import ast
from dataclasses import dataclass
from typing import Final, TypedDict

ARCH_NAME: Final = "__arch__"
REQUIRED_KEYS: Final = frozenset({"stage", "role", "owns", "not_owns"})


class ArchSidecar(TypedDict):
    stage: str
    role: str
    owns: list[str]
    not_owns: list[str]


@dataclass(frozen=True, slots=True)
class InvalidArchSidecarError(ValueError):
    path: str
    reason: str

    def __str__(self) -> str:
        return f"{self.path}: {self.reason}"


def _target_binds_arch(target: ast.AST) -> bool:
    if isinstance(target, ast.Name):
        return target.id == ARCH_NAME
    if isinstance(target, (ast.Tuple, ast.List)):
        return any(_target_binds_arch(element) for element in target.elts)
    if isinstance(target, ast.Starred):
        return _target_binds_arch(target.value)
    return False


def _pattern_binds_arch(pattern: ast.AST) -> bool:
    if isinstance(pattern, ast.MatchAs):
        return pattern.name == ARCH_NAME or (
            pattern.pattern is not None and _pattern_binds_arch(pattern.pattern)
        )
    if isinstance(pattern, ast.MatchStar):
        return pattern.name == ARCH_NAME
    if isinstance(pattern, ast.MatchMapping):
        return pattern.rest == ARCH_NAME or any(
            _pattern_binds_arch(item) for item in pattern.patterns
        )
    if isinstance(pattern, ast.MatchClass):
        return any(_pattern_binds_arch(item) for item in pattern.patterns) or any(
            _pattern_binds_arch(item) for item in pattern.kwd_patterns
        )
    if isinstance(pattern, (ast.MatchSequence, ast.MatchOr)):
        return any(_pattern_binds_arch(item) for item in pattern.patterns)
    return False


class _ModuleScopeBindingVisitor(ast.NodeVisitor):
    def __init__(self) -> None:
        self.reasons: list[str] = []

    def _reject(self, reason: str) -> None:
        self.reasons.append(reason)

    def visit_Assign(self, node: ast.Assign) -> None:
        if any(_target_binds_arch(target) for target in node.targets):
            self._reject("__arch__는 모듈 최상위의 단일 target Assign만 허용합니다")
        self.visit(node.value)

    def visit_AnnAssign(self, node: ast.AnnAssign) -> None:
        if _target_binds_arch(node.target):
            self._reject("__arch__의 AnnAssign는 허용되지 않습니다")
        if node.value is not None:
            self.visit(node.value)
        self.visit(node.annotation)

    def visit_AugAssign(self, node: ast.AugAssign) -> None:
        if _target_binds_arch(node.target):
            self._reject("__arch__의 AugAssign는 허용되지 않습니다")
        self.visit(node.value)

    def visit_NamedExpr(self, node: ast.NamedExpr) -> None:
        if _target_binds_arch(node.target):
            self._reject("__arch__의 NamedExpr는 허용되지 않습니다")
        self.visit(node.value)

    def _visit_for(self, node: ast.For | ast.AsyncFor) -> None:
        if _target_binds_arch(node.target):
            self._reject("__arch__의 for target은 허용되지 않습니다")
        self.visit(node.iter)
        for statement in node.body:
            self.visit(statement)
        for statement in node.orelse:
            self.visit(statement)

    def visit_For(self, node: ast.For) -> None:
        self._visit_for(node)

    def visit_AsyncFor(self, node: ast.AsyncFor) -> None:
        self._visit_for(node)

    def _visit_comprehension(
        self,
        generators: list[ast.comprehension],
        expressions: list[ast.AST],
    ) -> None:
        for generator in generators:
            if _target_binds_arch(generator.target):
                self._reject("__arch__의 comprehension target은 허용되지 않습니다")
            self.visit(generator.iter)
            for condition in generator.ifs:
                self.visit(condition)
        for expression in expressions:
            self.visit(expression)

    def visit_ListComp(self, node: ast.ListComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_SetComp(self, node: ast.SetComp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_GeneratorExp(self, node: ast.GeneratorExp) -> None:
        self._visit_comprehension(node.generators, [node.elt])

    def visit_DictComp(self, node: ast.DictComp) -> None:
        self._visit_comprehension(node.generators, [node.key, node.value])

    def _visit_with(self, node: ast.With | ast.AsyncWith) -> None:
        for item in node.items:
            if item.optional_vars is not None and _target_binds_arch(item.optional_vars):
                self._reject("__arch__의 with-as target은 허용되지 않습니다")
            self.visit(item.context_expr)
        for statement in node.body:
            self.visit(statement)

    def visit_With(self, node: ast.With) -> None:
        self._visit_with(node)

    def visit_AsyncWith(self, node: ast.AsyncWith) -> None:
        self._visit_with(node)

    def visit_ExceptHandler(self, node: ast.ExceptHandler) -> None:
        if node.name == ARCH_NAME:
            self._reject("__arch__의 except-as target은 허용되지 않습니다")
        if node.type is not None:
            self.visit(node.type)
        for statement in node.body:
            self.visit(statement)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            imported_root = alias.name.split(".", maxsplit=1)[0]
            if alias.asname == ARCH_NAME or (
                alias.asname is None and imported_root == ARCH_NAME
            ):
                self._reject("__arch__의 import alias는 허용되지 않습니다")

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        for alias in node.names:
            if alias.asname == ARCH_NAME or (
                alias.asname is None and alias.name == ARCH_NAME
            ):
                self._reject("__arch__의 import alias는 허용되지 않습니다")

    def visit_Match(self, node: ast.Match) -> None:
        self.visit(node.subject)
        for case in node.cases:
            if _pattern_binds_arch(case.pattern):
                self._reject("__arch__의 match capture는 허용되지 않습니다")
            if case.guard is not None:
                self.visit(case.guard)
            for statement in case.body:
                self.visit(statement)

    def _visit_function_def(
        self,
        node: ast.FunctionDef | ast.AsyncFunctionDef,
    ) -> None:
        if node.name == ARCH_NAME:
            self._reject("__arch__를 함수 이름으로 선언할 수 없습니다")
        for decorator in node.decorator_list:
            self.visit(decorator)
        self.visit(node.args)
        if node.returns is not None:
            self.visit(node.returns)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._visit_function_def(node)

    def visit_AsyncFunctionDef(self, node: ast.AsyncFunctionDef) -> None:
        self._visit_function_def(node)

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        if node.name == ARCH_NAME:
            self._reject("__arch__를 클래스 이름으로 선언할 수 없습니다")
        for decorator in node.decorator_list:
            self.visit(decorator)
        for base in node.bases:
            self.visit(base)
        for keyword in node.keywords:
            self.visit(keyword.value)

    def visit_TypeAlias(self, node: ast.AST) -> None:
        name = getattr(node, "name", None)
        if isinstance(name, ast.Name) and name.id == ARCH_NAME:
            self._reject("__arch__의 type alias는 허용되지 않습니다")
        value = getattr(node, "value", None)
        if isinstance(value, ast.AST):
            self.visit(value)

    def visit_Lambda(self, node: ast.Lambda) -> None:
        self.visit(node.args)


def _duplicate_dict_key(node: ast.Dict) -> str | None:
    seen: set[str] = set()
    for key in node.keys:
        if not isinstance(key, ast.Constant) or not isinstance(key.value, str):
            continue
        if key.value in seen:
            return key.value
        seen.add(key.value)
    return None


def _string_list_error(name: str, value: list[str]) -> str | None:
    if any(not item for item in value):
        return f"{name}는 비어 있지 않은 문자열 목록이어야 합니다"
    if len(value) != len(set(value)):
        return f"{name}에는 중복 항목이 없어야 합니다"
    return None


def extract_arch_sidecar(
    tree: ast.Module,
    path: str,
    expected_stage: str,
) -> ArchSidecar | None:
    """모듈을 실행하지 않고 허용된 `__arch__` literal sidecar를 추출합니다."""
    visitor = _ModuleScopeBindingVisitor()
    assignments: list[ast.Assign] = []
    for node in tree.body:
        if (
            isinstance(node, ast.Assign)
            and len(node.targets) == 1
            and isinstance(node.targets[0], ast.Name)
            and node.targets[0].id == ARCH_NAME
        ):
            assignments.append(node)
            visitor.visit(node.value)
        else:
            visitor.visit(node)

    if visitor.reasons:
        raise InvalidArchSidecarError(path, visitor.reasons[0])
    if not assignments:
        return None
    if len(assignments) != 1:
        raise InvalidArchSidecarError(path, "__arch__는 한 번만 선언해야 합니다")

    assignment = assignments[0]
    if not isinstance(assignment.value, ast.Dict):
        raise InvalidArchSidecarError(path, "__arch__는 literal dict여야 합니다")
    duplicate_key = _duplicate_dict_key(assignment.value)
    if duplicate_key is not None:
        raise InvalidArchSidecarError(
            path, f"__arch__ key가 중복되었습니다: {duplicate_key}"
        )
    try:
        raw = ast.literal_eval(assignment.value)
    except (TypeError, ValueError) as exc:
        raise InvalidArchSidecarError(
            path, "__arch__ 값은 literal이어야 합니다"
        ) from exc
    if not isinstance(raw, dict):
        raise InvalidArchSidecarError(path, "__arch__는 dict literal이어야 합니다")
    if set(raw) != REQUIRED_KEYS:
        missing = sorted(REQUIRED_KEYS - set(raw))
        extra = [key for key in raw if key not in REQUIRED_KEYS]
        raise InvalidArchSidecarError(
            path,
            f"__arch__ key는 네 개만 허용됩니다 (누락: {missing}, 추가: {extra})",
        )

    stage = raw["stage"]
    role = raw["role"]
    owns = raw["owns"]
    not_owns = raw["not_owns"]
    if not isinstance(stage, str) or not stage:
        raise InvalidArchSidecarError(
            path, "stage는 비어 있지 않은 문자열이어야 합니다"
        )
    if not isinstance(role, str) or not role:
        raise InvalidArchSidecarError(path, "role은 비어 있지 않은 문자열이어야 합니다")
    if not isinstance(owns, list) or not all(isinstance(item, str) for item in owns):
        raise InvalidArchSidecarError(path, "owns는 문자열 목록이어야 합니다")
    if not isinstance(not_owns, list) or not all(
        isinstance(item, str) for item in not_owns
    ):
        raise InvalidArchSidecarError(path, "not_owns는 문자열 목록이어야 합니다")
    for name, value in (("owns", owns), ("not_owns", not_owns)):
        reason = _string_list_error(name, value)
        if reason is not None:
            raise InvalidArchSidecarError(path, reason)
    if stage != expected_stage:
        raise InvalidArchSidecarError(path, f"stage는 {expected_stage!r}이어야 합니다")
    return {"stage": stage, "role": role, "owns": owns, "not_owns": not_owns}
