"""AST-based invariant tests for class complexity ceilings and empty logging functions.

The complexity ceiling is a ratchet rather than a catastrophe tripwire: it sits
just above the largest class in the tree, so ordinary growth is what reddens the
scan, and `test_the_ceiling_stays_within_reach_of_the_largest_class` keeps it
there. A class that has to be bigger moves the ceiling in a reviewed diff;
there is no per-class exemption decorator.
"""

from __future__ import annotations

import ast
from pathlib import Path
from typing import NamedTuple

import pytest

pytestmark = pytest.mark.repo

REPO_ROOT = Path(__file__).resolve().parent.parent.parent
SRC_DIRS = [
    REPO_ROOT / "src",
    *(REPO_ROOT / "packages").glob("*/src"),
]

# Just above the largest class in src/ and packages/*/src, so growth in the
# biggest classes is what reddens the scan.
CLASS_STATEMENT_CEILING = 360

# How far the ceiling may sit above the largest class actually measured. Wide
# enough that decomposing a class is not a build break, narrow enough that a
# ceiling raised out of the way of the code it measures is.
CEILING_HEADROOM = 100


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


#: Methods that return a logger bound to extra context: `logger.bind(k=v).info(...)` logs through
#: the same logger, so the receiver of the final call is read through them.
_LOGGER_REBINDERS = {"bind", "opt"}
_LOGGER_NAMES = {"logger", "log", "logging", "_logger", "_log"}
_LOG_METHODS = {"debug", "info", "warning", "warn", "error", "critical", "exception", "log"}


def _is_a_logger(recv: ast.expr) -> bool:
    """Whether an expression is a logger: a known name, an attribute of one, or either re-bound."""
    while (
        isinstance(recv, ast.Call)
        and isinstance(recv.func, ast.Attribute)
        and recv.func.attr in _LOGGER_REBINDERS
    ):
        recv = recv.func.value
    if isinstance(recv, ast.Name):
        return recv.id in _LOGGER_NAMES
    if isinstance(recv, ast.Attribute):
        if recv.attr in _LOGGER_NAMES - {"logging"}:
            return True
        return isinstance(recv.value, ast.Name) and recv.value.id == "logging"
    return False


def is_logging_call(stmt: ast.stmt) -> bool:
    """Check if a statement is an expression calling a logger method."""
    if not isinstance(stmt, ast.Expr):
        return False
    if not isinstance(stmt.value, ast.Call):
        return False
    func = stmt.value.func
    return (
        isinstance(func, ast.Attribute) and func.attr in _LOG_METHODS and _is_a_logger(func.value)
    )


def has_no_effect(stmt: ast.stmt) -> bool:
    """Check whether a statement is one of the recognised do-nothing endings.

    A stub that logs and then falls off the end is the same stub whether it
    spells the ending `pass`, `return`, `return None`, `...`, or a constant
    return, so none of those count as business logic. Docstrings land here too.

    This is a shape rule, not dead-code analysis: a statement that merely looks
    like work, such as an assignment to an unused name, is not recognised.
    """
    if isinstance(stmt, ast.Pass):
        return True
    if isinstance(stmt, ast.Return):
        return stmt.value is None or isinstance(stmt.value, ast.Constant)
    return isinstance(stmt, ast.Expr) and isinstance(stmt.value, ast.Constant)


def is_empty_logging_function(func_node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    """Check if a function's body consists solely of logging calls without business logic.

    The do-nothing endings `has_no_effect` recognises are ignored alongside the
    docstring, so a stub cannot hide behind one of them. Returns True if
    everything that remains is a logger call and at least one is present.
    """
    effective_stmts = [s for s in func_node.body if not has_no_effect(s)]

    if not effective_stmts:
        return False

    return all(is_logging_call(s) for s in effective_stmts)


#: The files that legitimately wrap a logger, by repository-relative path. A whole package is not
#: exempt: a stub added to another module of it would be invisible. Each entry has to exist and
#: to suppress a real finding (`test_control_every_logging_exemption_is_live`), so a rename or a
#: rewrite that makes one unnecessary is a failure and not a stale line.
EXEMPT_LOGGING_MODULES = ("packages/cliffracer-logging/src/cliffracer_logging/config.py",)


def is_exempt_logging_module(file_path: Path) -> bool:
    """Check whether a file is one of the logging utility modules that wrap a logger on purpose."""
    try:
        relative = file_path.resolve().relative_to(REPO_ROOT).as_posix()
    except ValueError:
        return False
    return relative in EXEMPT_LOGGING_MODULES


def check_source_complexity(
    source_code: str, file_path: str = "<source>", ceiling: int = CLASS_STATEMENT_CEILING
) -> list[ClassComplexityViolation]:
    """Parse source and return all classes violating the statement ceiling.

    There is no per-class exemption. A class that has to be bigger than the
    ceiling is a reason to move the ceiling, in a diff a reviewer reads.
    """
    tree = ast.parse(source_code, filename=file_path)
    violations: list[ClassComplexityViolation] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            stmt_count = count_ast_statements(node)
            if stmt_count > ceiling:
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


def largest_class_measured() -> tuple[int, str]:
    """Return the biggest class in src/ and packages/*/src as (statements, where)."""
    biggest = (0, "<none>")
    for src_dir in SRC_DIRS:
        for py_file in src_dir.rglob("*.py"):
            tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef):
                    count = count_ast_statements(node)
                    if count > biggest[0]:
                        where = f"{node.name} in {py_file.relative_to(REPO_ROOT)}:{node.lineno}"
                        biggest = (count, where)
    return biggest


def test_no_class_exceeds_the_statement_ceiling():
    """Invariant: no class in src/ or packages/*/src/ exceeds the statement ceiling.

    A class over the ceiling is decomposed, or the ceiling is raised in this
    file with the reason in the diff. There is no per-class escape hatch.
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

            file_violations = check_source_complexity(
                content, file_path=str(py_file.relative_to(REPO_ROOT))
            )
            all_violations.extend(file_violations)

    assert scanned_classes > 20, f"Expected to scan dozens of classes, found {scanned_classes}"
    if all_violations:
        biggest, where = largest_class_measured()
        msg_lines = [
            f"Found {len(all_violations)} class(es) exceeding the "
            f"{CLASS_STATEMENT_CEILING} AST statement ceiling "
            f"(largest measured: {biggest} statements, {where}):"
        ]
        for v in all_violations:
            msg_lines.append(
                f"  - {v.class_name} in {v.file_path}:{v.line_number} (statements: {v.statement_count})"
            )
        msg_lines.append(
            f"\nResolution: decompose the class, or raise CLASS_STATEMENT_CEILING "
            f"above {biggest} in {Path(__file__).name} and say why in the diff."
        )
        pytest.fail("\n".join(msg_lines))


def test_the_ceiling_stays_within_reach_of_the_largest_class():
    """The ceiling tracks the code, so it cannot be raised out of the way of it.

    A ceiling far above everything it measures is green no matter how the tree
    grows, which is the state this ratchet exists to leave.
    """
    biggest, where = largest_class_measured()
    assert biggest > 0, "no classes were measured at all"
    assert CLASS_STATEMENT_CEILING >= biggest, (
        f"the ceiling is {CLASS_STATEMENT_CEILING} but {where} already measures "
        f"{biggest} statements"
    )
    assert CLASS_STATEMENT_CEILING - biggest <= CEILING_HEADROOM, (
        f"the ceiling is {CLASS_STATEMENT_CEILING}, {CLASS_STATEMENT_CEILING - biggest} "
        f"above the largest class measured ({biggest} statements, {where}). "
        f"At most {CEILING_HEADROOM} of headroom keeps this a ratchet; lower the "
        f"ceiling or say in the diff why it moved."
    )


def test_no_empty_logging_functions_in_production_code():
    """Invariant: Catch fake stubs that merely log without executing logic.

    Detects functions whose body is logger.<level>() calls plus do-nothing
    endings, so closing the stub with `pass`, `return`, `return None`, `...` or
    a constant return does not hide it. A stub padded with a statement that
    looks like work is still out of reach. Exempts dedicated logging utility
    modules (e.g. in cliffracer_logging/).
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

            file_violations = check_empty_logging_functions(
                content, file_path=str(py_file.relative_to(REPO_ROOT))
            )
            all_violations.extend(file_violations)

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
    """Negative control: Verify functions that do real work pass."""
    genuine_code = '''
def revoke_token(self, token: str) -> None:
    """Revoke a JWT token by adding its identifier to the in-memory revoked set."""
    logger.info("Token revoked")
    self._revoked_jtis.add(token)

def get_status(self) -> str:
    """Return status."""
    logger.debug("Checking status")
    return self._status

async def async_dispatch(self, msg: dict) -> None:
    """Dispatch message."""
    self.logger.info("Dispatching")
    await self._do_work(msg)
'''
    violations = check_empty_logging_functions(genuine_code)
    assert len(violations) == 0, f"Expected no violations, found {violations}"


@pytest.mark.parametrize(
    "ending",
    ["    return None", "    return", "    pass", "    ...", '    return "ok"'],
)
def test_control_empty_logging_linter_catches_a_stub_however_it_ends(ending: str):
    """Positive control: a trailing statement with no effect does not hide a stub."""
    fake_code = (
        "def revoke_all_tokens(token: str) -> None:\n"
        '    """Revoke every issued token by clearing the in-memory revoked set."""\n'
        '    logger.info("all tokens revoked")\n'
        f"{ending}\n"
    )
    violations = check_empty_logging_functions(fake_code)
    assert len(violations) == 1, f"{ending!r} hid the stub: {violations}"
    assert violations[0].function_name == "revoke_all_tokens"


def test_control_a_function_that_only_does_nothing_is_not_a_logging_stub():
    """Negative control: no logging call means this detector has nothing to say."""
    quiet_code = '''
def not_implemented_yet(self) -> None:
    """Deliberately does nothing."""
    pass

def also_nothing(self) -> None:
    ...
'''
    violations = check_empty_logging_functions(quiet_code)
    assert len(violations) == 0, f"Expected no violations, found {violations}"


def _real_source_files() -> list[Path]:
    return [path for src_dir in SRC_DIRS for path in src_dir.rglob("*.py")]


def _findings_in(path: Path) -> list[EmptyLoggingViolation]:
    return check_empty_logging_functions(path.read_text(encoding="utf-8"), file_path=str(path))


def test_control_every_logging_exemption_is_live():
    """Each exempt file exists and, unexempted, has a finding: a rename or a rewrite that leaves
    the exemption suppressing nothing is a failure, not a stale line that exempts the next stub."""
    for relative in EXEMPT_LOGGING_MODULES:
        path = REPO_ROOT / relative
        assert path.is_file(), f"{relative} is exempt and does not exist"
        assert path in _real_source_files(), f"{relative} is exempt but is not scanned"
        assert _findings_in(path), f"{relative} is exempt and has nothing to be exempt from"


def test_control_the_exemption_is_one_file_and_not_its_package():
    """Read from the real tree: the exempt file is exempt, its neighbours in the same package and
    a module of another package are not."""
    exempt = REPO_ROOT / EXEMPT_LOGGING_MODULES[0]
    neighbours = [p for p in exempt.parent.rglob("*.py") if p != exempt]
    assert neighbours, "the exempt file has no neighbour in its package"
    assert is_exempt_logging_module(exempt)
    assert not any(is_exempt_logging_module(p) for p in neighbours)
    assert not is_exempt_logging_module(
        REPO_ROOT / "packages/cliffracer-auth/src/cliffracer_auth/simple_auth.py"
    )
    assert not is_exempt_logging_module(REPO_ROOT / "src/cliffracer/core/service.py")


def test_control_a_path_that_only_looks_like_the_exempt_one_is_not_exempt(tmp_path: Path):
    lookalike = tmp_path / EXEMPT_LOGGING_MODULES[0]
    lookalike.parent.mkdir(parents=True)
    lookalike.write_text("")

    assert not is_exempt_logging_module(lookalike)


def test_control_repo_invariant_detection_flags_violations():
    """Load-bearing control: Ensure check_source_complexity directly gates the repository scan."""
    oversized = "class Oversized:\n" + "\n".join(f"    def m_{i}(self): pass" for i in range(260))
    violations = check_source_complexity(oversized, file_path="synthetic.py", ceiling=500)
    assert len(violations) == 1
    assert violations[0].class_name == "Oversized"

    empty_log = "def empty_stub():\n    logger.info('nothing else here')"
    violations = check_empty_logging_functions(empty_log, file_path="synthetic.py")
    assert len(violations) == 1
    assert violations[0].function_name == "empty_stub"


@pytest.mark.parametrize(
    "body",
    [
        "    self._log.info('x')",
        "    self.logger.bind(k=1).info('x')",
        "    logger.bind(k=1).opt(depth=1).warning('x')",
        "    logging.info('x')",
        "    self._logger.bind(**kwargs).debug(message)",
    ],
)
def test_control_the_detector_recognises_a_stub_logging_through_these_receivers(body: str):
    source = f"def stub(self):\n{body}\n"

    assert [v.function_name for v in check_empty_logging_functions(source)] == ["stub"]


@pytest.mark.parametrize(
    "body",
    [
        "    self.registry.info('x')",
        "    self.client.bind(k=1).info('x')",
        "    notes.bind(k=1).info('x')",
        "    make_logger().info('x')",
        "    loggers[0].info('x')",
        "    self._log.info('x')\n    self.count += 1",
    ],
)
def test_control_the_detector_leaves_alone_a_call_that_is_not_a_logger_or_has_other_work(body: str):
    source = f"def real(self):\n{body}\n"

    assert check_empty_logging_functions(source) == []
