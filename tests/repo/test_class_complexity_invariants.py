"""AST-based invariant tests for class complexity ceilings and empty logging functions."""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

from cliffracer.invariants import override_length_check

pytestmark = pytest.mark.repo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIRS = [
    REPO_ROOT / "src",
    *(REPO_ROOT / "packages").glob("*/src"),
]


class ClassComplexityViolation(NamedTuple):
    class_name: str
    file_path: str
    line_number: int
    statement_count: int


class EmptyLoggingViolation(NamedTuple):
    function_name: str
    file_path: str
    line_number: int
    statement_count: int


def count_ast_statements(node: ast.AST) -> int:
    """Count all AST statement nodes (isinstance(n, ast.stmt)) strictly contained in node."""
    return sum(1 for child in ast.walk(node) if isinstance(child, ast.stmt) and child is not node)


def extract_override_reason(class_node: ast.ClassDef) -> str | None:
    """Extract non-empty exemption reason if decorated with @override_length_check."""
    for decorator in class_node.decorator_list:
        # Decorator could be @override_length_check(reason="...") or @invariants.override_length_check(...)
        call_node: ast.Call | None = None
        if isinstance(decorator, ast.Call):
            call_node = decorator

        if call_node is None:
            continue

        func = call_node.func
        name = ""
        if isinstance(func, ast.Name):
            name = func.id
        elif isinstance(func, ast.Attribute):
            name = func.attr

        if name == "override_length_check":
            # Check keyword argument reason="string"
            for kw in call_node.keywords:
                if kw.arg == "reason":
                    if isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
                        reason = kw.value.value.strip()
                        if reason:
                            return reason

            # Check positional argument override_length_check("string")
            if call_node.args:
                first_arg = call_node.args[0]
                if isinstance(first_arg, ast.Constant) and isinstance(first_arg.value, str):
                    reason = first_arg.value.strip()
                    if reason:
                        return reason

    return None


def is_logging_call(stmt: ast.stmt) -> bool:
    """Check if a statement is an expression calling a logger method."""
    if not isinstance(stmt, ast.Expr):
        return False
    if not isinstance(stmt.value, ast.Call):
        return False
    call = stmt.value
    func = call.func
    if isinstance(func, ast.Attribute):
        log_methods = {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}
        if func.attr in log_methods:
            # Check logger receiver: logger.xxx, self.logger.xxx, logging.xxx
            recv = func.value
            if isinstance(recv, ast.Name) and recv.id in {"logger", "log", "logging", "_logger"}:
                return True
            if isinstance(recv, ast.Attribute) and recv.attr in {"logger", "log", "_logger"}:
                return True
            if (
                isinstance(recv, ast.Attribute)
                and isinstance(recv.value, ast.Name)
                and recv.value.id == "logging"
            ):
                return True
    return False


def is_empty_logging_function(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function's body consists solely of logging calls without business logic.

    Excludes docstrings and doc-comments. Returns True if all non-docstring
    statements are logger calls and at least one logger call exists.
    """
    non_docstring_stmts: list[ast.stmt] = []
    for stmt in func_node.body:
        # Ignore docstrings
        if (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and isinstance(stmt.value.value, str)
        ):
            continue
        non_docstring_stmts.append(stmt)

    if not non_docstring_stmts:
        return False

    return all(is_logging_call(s) for s in non_docstring_stmts)


def is_exempt_logging_module(file_path: Path) -> bool:
    """Check whether a file is a dedicated logging utility module that legitimately wraps loggers."""
    path_str = str(file_path).replace("\\", "/")
    exempt_markers = [
        "cliffracer-logging/",
        "cliffracer_logging/",
        "correlation_logging.py",
    ]
    return any(marker in path_str for marker in exempt_markers)


def check_source_complexity(
    source_code: str, file_path: str = "<source>", ceiling: int = 500
) -> list[ClassComplexityViolation]:
    """Parse source and return all classes violating the statement ceiling."""
    tree = ast.parse(source_code, filename=file_path)
    violations: list[ClassComplexityViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            stmt_count = count_ast_statements(node)
            if stmt_count > ceiling:
                reason = extract_override_reason(node)
                if not reason:
                    violations.append(
                        ClassComplexityViolation(
                            class_name=node.name,
                            file_path=file_path,
                            line_number=node.lineno,
                            statement_count=stmt_count,
                        )
                    )
    return violations


def check_empty_logging_functions(
    source_code: str, file_path: str = "<source>"
) -> list[EmptyLoggingViolation]:
    """Parse source and return all empty logging functions."""
    tree = ast.parse(source_code, filename=file_path)
    violations: list[EmptyLoggingViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if is_empty_logging_function(node):
                violations.append(
                    EmptyLoggingViolation(
                        function_name=node.name,
                        file_path=file_path,
                        line_number=node.lineno,
                        statement_count=len(node.body),
                    )
                )
    return violations


# ==============================================================================
# Repository-wide AST Invariant Tests
# ==============================================================================


def test_no_classes_exceed_500_ast_statements_without_override():
    """Invariant: No class in src/ or packages/*/src/ exceeds 500 AST statement nodes.

    Classes exceeding the ceiling must be decomposed or explicitly decorated
    with @override_length_check(reason="...").
    """
    all_violations: list[ClassComplexityViolation] = []
    scanned_classes = 0

    for src_dir in SRC_DIRS:
        assert src_dir.is_dir(), f"Expected source directory does not exist: {src_dir}"
        for py_file in src_dir.rglob("*.py"):
            try:
                content = py_file.read_text(encoding="utf-8")
                tree = ast.parse(content, filename=str(py_file))
            except Exception as exc:
                pytest.fail(f"Failed to parse {py_file}: {exc}")

            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    scanned_classes += 1
                    stmt_count = count_ast_statements(node)
                    if stmt_count > 500:
                        reason = extract_override_reason(node)
                        if not reason:
                            all_violations.append(
                                ClassComplexityViolation(
                                    class_name=node.name,
                                    file_path=str(py_file.relative_to(REPO_ROOT)),
                                    line_number=node.lineno,
                                    statement_count=stmt_count,
                                )
                            )

    assert scanned_classes > 20, f"Expected to scan dozens of classes, found {scanned_classes}"
    if all_violations:
        msg_lines = [
            f"Found {len(all_violations)} class(es) exceeding the 500 AST statement ceiling without @override_length_check:"
        ]
        for v in all_violations:
            msg_lines.append(
                f"  - {v.class_name} in {v.file_path}:{v.line_number} (statements: {v.statement_count})"
            )
        msg_lines.append(
            "\nResolution: Decompose the class or explicitly add @override_length_check(reason='...')"
        )
        pytest.fail("\n".join(msg_lines))


def test_no_empty_logging_functions_in_production_code():
    """Invariant: Catch fake stubs that merely log without executing logic.

    Detects functions whose non-docstring body consists solely of logger.<level>() calls.
    Exempts dedicated logging utility modules (e.g. in cliffracer_logging/).
    """
    all_violations: list[EmptyLoggingViolation] = []
    scanned_functions = 0

    for src_dir in SRC_DIRS:
        for py_file in src_dir.rglob("*.py"):
            if is_exempt_logging_module(py_file):
                continue

            try:
                content = py_file.read_text(encoding="utf-8")
                tree = ast.parse(content, filename=str(py_file))
            except Exception as exc:
                pytest.fail(f"Failed to parse {py_file}: {exc}")

            for node in ast.walk(tree):
                if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
                    scanned_functions += 1
                    if is_empty_logging_function(node):
                        all_violations.append(
                            EmptyLoggingViolation(
                                function_name=node.name,
                                file_path=str(py_file.relative_to(REPO_ROOT)),
                                line_number=node.lineno,
                                statement_count=len(node.body),
                            )
                        )

    assert scanned_functions > 100, f"Expected to scan >100 functions, found {scanned_functions}"
    if all_violations:
        msg_lines = [f"Found {len(all_violations)} empty logging function(s) in production code:"]
        for v in all_violations:
            msg_lines.append(f"  - {v.function_name}() at {v.file_path}:{v.line_number}")
        msg_lines.append("\nResolution: Implement genuine business logic or remove fake stub.")
        pytest.fail("\n".join(msg_lines))


# ==============================================================================
# Positive and Negative Controls for AST Linters
# ==============================================================================


def test_control_complexity_linter_catches_oversized_class():
    """Positive control: Ensure class complexity linter flags classes > 500 statements."""
    methods = "\n".join(
        f"    def method_{i}(self):\n        x = {i}\n        return x" for i in range(260)
    )
    oversized_code = f"class GiantService:\n{methods}\n"

    violations = check_source_complexity(oversized_code, ceiling=500)
    assert len(violations) == 1
    assert violations[0].class_name == "GiantService"
    assert violations[0].statement_count > 500


def test_control_complexity_linter_exempts_with_valid_override_decorator():
    """Negative control: Verify @override_length_check(reason=...) exempts oversized class."""
    methods = "\n".join(
        f"    def method_{i}(self):\n        x = {i}\n        return x" for i in range(260)
    )
    exempted_code = (
        "from cliffracer.invariants import override_length_check\n\n"
        "@override_length_check(reason='Approved monolithic protocol state machine')\n"
        f"class GiantService:\n{methods}\n"
    )

    violations = check_source_complexity(exempted_code, ceiling=500)
    assert len(violations) == 0, "Expected @override_length_check to exempt the class"


def test_control_complexity_linter_rejects_empty_or_missing_reason_override():
    """Ensure @override_length_check without a reason or with an empty reason is rejected."""
    methods = "\n".join(
        f"    def method_{i}(self):\n        x = {i}\n        return x" for i in range(260)
    )
    empty_reason_code = f"@override_length_check(reason='   ')\nclass GiantService:\n{methods}\n"
    violations = check_source_complexity(empty_reason_code, ceiling=500)
    assert len(violations) == 1

    no_args_code = f"@override_length_check()\nclass GiantService:\n{methods}\n"
    violations_no_args = check_source_complexity(no_args_code, ceiling=500)
    assert len(violations_no_args) == 1


def test_control_empty_logging_linter_catches_fake_functions():
    """Positive control: Verify empty logging function detector catches fake stubs."""
    fake_code = '''
def revoke_token(self, token: str) -> None:
    """Revoke a JWT token by adding its identifier to the in-memory revoked set."""
    logger.info("Token revoked")
'''
    violations = check_empty_logging_functions(fake_code)
    assert len(violations) == 1
    assert violations[0].function_name == "revoke_token"


def test_control_empty_logging_linter_allows_genuine_functions():
    """Negative control: Verify functions with business logic or return statements pass."""
    genuine_code = '''
def revoke_token(self, token: str) -> None:
    """Revoke a JWT token by adding its identifier to the in-memory revoked set."""
    logger.info("Token revoked")
    self._revoked_jtis.add(token)

def get_status(self) -> str:
    """Return status."""
    logger.debug("Checking status")
    return "ok"

async def async_dispatch(self, msg: dict) -> None:
    """Dispatch message."""
    self.logger.info("Dispatching")
    await self._do_work(msg)
'''
    violations = check_empty_logging_functions(genuine_code)
    assert len(violations) == 0, f"Expected no violations, found {violations}"


def test_control_logging_utility_module_exemption():
    """Verify dedicated logging modules are exempted from empty logging checks."""
    assert is_exempt_logging_module(
        Path("/path/to/packages/cliffracer-logging/src/cliffracer_logging/correlation_logging.py")
    )
    assert is_exempt_logging_module(
        Path("/path/to/packages/cliffracer-logging/src/cliffracer_logging/config.py")
    )
    assert not is_exempt_logging_module(
        Path("/path/to/packages/cliffracer-auth/src/cliffracer_auth/simple_auth.py")
    )
    assert not is_exempt_logging_module(Path("/path/to/src/cliffracer/core/service.py"))


def test_override_length_check_decorator_runtime_behavior():
    """Verify runtime validation and attribute assignment of @override_length_check."""

    # Test valid decorator usage
    @override_length_check(reason="Architectural exception documented in ADR-042")
    class SampleClass:
        pass

    assert (
        SampleClass.__override_length_check_reason__  # type: ignore[attr-defined]
        == "Architectural exception documented in ADR-042"
    )

    # Test rejection of empty or non-string reasons
    with pytest.raises(ValueError, match="requires a non-empty reason string"):
        override_length_check(reason="")

    with pytest.raises(ValueError, match="requires a non-empty reason string"):
        override_length_check(reason="   ")

    with pytest.raises(ValueError, match="requires a non-empty reason string"):
        override_length_check(reason=None)  # type: ignore[arg-type]
