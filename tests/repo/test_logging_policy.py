"""Guard test: enforce loguru-only logging in library code.

Uses AST so docstring examples and comments never false-positive — only real
`import logging` statements and real `print(...)` calls are flagged. REPL/CLI
modules that legitimately write to a user's terminal are exempt.
"""

import ast
import pathlib

import pytest

pytestmark = pytest.mark.repo

ROOT = pathlib.Path(__file__).resolve().parents[2]
SRC = ROOT / "src" / "cliffracer"
PACKAGES = ROOT / "packages"

# Modules whose print() output is legitimate user-facing terminal output
# (CLI commands).
EXEMPT_PRINT = {
    # CLI client generator outputs directly to standard streams.
    SRC / "generate_client" / "cli.py",
    # The dead-letter inspector prints what it reads to standard output.
    ROOT / "packages" / "cliffracer-dlq" / "src" / "cliffracer_dlq" / "cli.py",
}


def _py_files():
    """Collect Python files across core and workspace packages."""
    return sorted(SRC.rglob("*.py")) + sorted(ROOT.glob("packages/*/src/**/*.py"))


def is_dynamic_stdlib_logging_call(node: ast.Call) -> bool:
    """Check for __import__('logging') or importlib.import_module('logging')."""
    if isinstance(node.func, ast.Name) and node.func.id == "__import__":
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            val = node.args[0].value
            if val == "logging" or val.startswith("logging."):
                return True
    elif isinstance(node.func, ast.Attribute) and node.func.attr == "import_module":
        if (
            node.args
            and isinstance(node.args[0], ast.Constant)
            and isinstance(node.args[0].value, str)
        ):
            val = node.args[0].value
            if val == "logging" or val.startswith("logging."):
                return True
    return False


def test_the_logging_sweep_reads_the_library():
    """Verify file discovery count and scanned statement floors across the codebase."""
    files = _py_files()
    assert len(files) >= 85, f"only found {len(files)} library python files"

    src_files = [f for f in files if SRC in f.parents or f == SRC]
    assert len(src_files) >= 45, f"only found {len(src_files)} core python files in {SRC}"

    package_dirs = sorted(p for p in PACKAGES.iterdir() if (p / "pyproject.toml").exists())
    assert len(package_dirs) >= 7, f"expected at least 7 packages, found {len(package_dirs)}"
    for pkg in package_dirs:
        pkg_files = [f for f in files if pkg in f.parents]
        assert pkg_files, f"package {pkg.name} contributed no files to the logging sweep"

    total_statements = sum(
        sum(1 for node in ast.walk(ast.parse(f.read_text())) if isinstance(node, ast.stmt))
        for f in files
    )
    assert total_statements >= 7000, (
        f"only scanned {total_statements} statements across library files"
    )


def test_no_stdlib_logging_in_src():
    files = _py_files()
    assert len(files) >= 85, f"only found {len(files)} files to inspect"
    statement_count = 0
    offenders = []
    for f in files:
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.stmt):
                statement_count += 1
            if isinstance(node, ast.Import) and any(
                a.name == "logging" or a.name.startswith("logging.") for a in node.names
            ):
                offenders.append(f"{f.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and (
                node.module == "logging" or (node.module or "").startswith("logging.")
            ):
                offenders.append(f"{f.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.Call) and is_dynamic_stdlib_logging_call(node):
                offenders.append(f"{f.relative_to(ROOT)}:{node.lineno} (dynamic import)")
    assert statement_count >= 7000, f"only scanned {statement_count} statements"
    assert offenders == [], f"stdlib logging used (use loguru instead): {offenders}"


def test_no_print_in_library_code():
    files = _py_files()
    assert len(files) >= 85, f"only found {len(files)} files to inspect"
    statement_count = 0
    offenders = []
    for f in files:
        if f in EXEMPT_PRINT:
            continue
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.stmt):
                statement_count += 1
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{f.relative_to(ROOT)}:{node.lineno}")
    assert statement_count >= 6500, f"only scanned {statement_count} statements"
    assert offenders == [], (
        f"print() used as logging (use loguru, or add to EXEMPT_PRINT if truly user-facing): {offenders}"
    )


def test_the_print_exemptions_all_exist():
    """An exemption keyed on a deleted path silently stops exempting.

    The comment on EXEMPT_PRINT already says so; nothing enforced it until now.
    Removing client_generator.py left an entry pointing at a file that is gone
    and the suite stayed green -- the entry was simply inert, and would have
    gone on reading as coverage for a file nobody could find.
    """
    missing = sorted(str(path) for path in EXEMPT_PRINT if not path.exists())
    assert not missing, f"EXEMPT_PRINT names files that do not exist: {missing}"

    # An exemption is for a file that prints. One that no longer does exempts nothing today and
    # waives the whole file for the next `print` someone adds to it as logging: the exemption is
    # per file, not per call, because the file's prints are its user-facing output.
    inert = sorted(
        str(path.relative_to(ROOT))
        for path in EXEMPT_PRINT
        if not any(
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "print"
            for node in ast.walk(ast.parse(path.read_text()))
        )
    )
    assert not inert, f"EXEMPT_PRINT names files that no longer call print: {inert}"


def test_CONTROL_dynamic_stdlib_logging_is_caught():
    """Control: Dynamic logging imports via __import__ and importlib are detected."""
    sample = (
        "import importlib\n"
        'logging = __import__("logging")\n'
        'mod = importlib.import_module("logging.handlers")\n'
    )
    tree = ast.parse(sample)
    calls = [
        node
        for node in ast.walk(tree)
        if isinstance(node, ast.Call) and is_dynamic_stdlib_logging_call(node)
    ]
    assert len(calls) == 2, f"Expected 2 dynamic logging imports detected, found {len(calls)}"
