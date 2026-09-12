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
# (interactive backdoor REPL, the NATS/service inspector, and CLI commands).
EXEMPT_PRINT = {
    # Backdoor package modules write directly to the terminal interface.
    PACKAGES / "cliffracer-backdoor" / "src" / "cliffracer_backdoor" / "backdoor.py",
    PACKAGES / "cliffracer-backdoor" / "src" / "cliffracer_backdoor" / "inspector.py",
    PACKAGES / "cliffracer-backdoor" / "src" / "cliffracer_backdoor" / "cli.py",
    # CLI client generator outputs directly to standard streams.
    SRC / "generate_client" / "cli.py",
}


def _py_files():
    """Collect Python files across core and workspace packages."""
    return sorted(SRC.rglob("*.py")) + sorted(ROOT.glob("packages/*/src/**/*.py"))


def test_no_stdlib_logging_in_src():
    offenders = []
    for f in _py_files():
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if isinstance(node, ast.Import) and any(
                a.name == "logging" or a.name.startswith("logging.") for a in node.names
            ):
                offenders.append(f"{f.relative_to(ROOT)}:{node.lineno}")
            elif isinstance(node, ast.ImportFrom) and (
                node.module == "logging" or (node.module or "").startswith("logging.")
            ):
                offenders.append(f"{f.relative_to(ROOT)}:{node.lineno}")
    assert offenders == [], f"stdlib logging used (use loguru instead): {offenders}"


def test_no_print_in_library_code():
    offenders = []
    for f in _py_files():
        if f in EXEMPT_PRINT:
            continue
        tree = ast.parse(f.read_text())
        for node in ast.walk(tree):
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "print"
            ):
                offenders.append(f"{f.relative_to(ROOT)}:{node.lineno}")
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
