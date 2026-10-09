"""Enforce ADR-0001 Persistence Boundary invariants across core package."""

import ast
import re
import tomllib
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

BANNED_PERSISTENCE_MODULES = {
    "alembic",
    "asyncpg",
    "databases",
    "motor",
    "peewee",
    "psycopg",
    "psycopg2",
    "pymongo",
    "redis",
    "sqlalchemy",
    "sqlite3",
    "tortoise",
}

# ADR-0001's third obligation is that a persistence layer written in pure
# Python, adding no dependency at all, still crosses the boundary. A list of
# exact names cannot carry that: it is red only for the spellings someone
# happened to write down, so the same code named `DataStore` or `UnitOfWork`
# walks past. These match the shapes a persistence type announces itself with.
#
# What this still cannot see: a persistence layer whose names say nothing about
# persistence. No name rule reaches that, and the dependency and import checks
# above are what cover the ordinary case of reaching for a database driver.
BANNED_PERSISTENCE_CLASS_PATTERN = re.compile(
    r"(?:Repository|Repo|DataStore|DataAccess|DAO|UnitOfWork|SessionManager"
    r"|MigrationRunner|Migrator|ConnectionPool|DatabasePool|Pool|Store)$"
)

BANNED_PERSISTENCE_FUNCTION_PATTERN = re.compile(
    r"^(?:create_pool|get_db_session|run_migrations|apply_migrations|migrate)$"
    r"|_migrations?$|^migrate_|_db_session$"
)


def _check_dependencies_clean(meta: dict) -> list[str]:
    violations: list[str] = []
    deps = meta.get("project", {}).get("dependencies", [])
    for dep in deps:
        name = dep.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip().lower()
        if name in BANNED_PERSISTENCE_MODULES:
            violations.append(f"dependencies: {dep}")

    optional_deps = meta.get("project", {}).get("optional-dependencies", {})
    for extra, extra_list in optional_deps.items():
        for dep in extra_list:
            name = dep.split(">")[0].split("=")[0].split("<")[0].split("[")[0].strip().lower()
            if name in BANNED_PERSISTENCE_MODULES:
                violations.append(f"optional-dependencies[{extra}]: {dep}")
    return violations


def _check_ast_clean(path: Path, tree: ast.AST) -> list[str]:
    violations: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0].lower()
                if root in BANNED_PERSISTENCE_MODULES:
                    violations.append(f"{path}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            root = node.module.split(".")[0].lower()
            if root in BANNED_PERSISTENCE_MODULES:
                violations.append(f"{path}:{node.lineno} imports from {node.module}")
        elif isinstance(node, ast.ClassDef):
            if BANNED_PERSISTENCE_CLASS_PATTERN.search(node.name):
                violations.append(f"{path}:{node.lineno} defines class {node.name}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if BANNED_PERSISTENCE_FUNCTION_PATTERN.search(node.name):
                violations.append(f"{path}:{node.lineno} defines function {node.name}")
    return violations


def test_core_dependencies_contain_no_persistence_libraries():
    """Verify pyproject.toml declares no database drivers or ORMs in dependencies or extras."""
    pyproject_path = REPO / "pyproject.toml"
    meta = tomllib.loads(pyproject_path.read_text())
    violations = _check_dependencies_clean(meta)
    assert not violations, f"Persistence dependencies found in pyproject.toml: {violations}"


def test_core_source_imports_no_persistence_libraries():
    """Verify core source files contain no top-level or lazy persistence library imports."""
    core_root = REPO / "src" / "cliffracer"
    python_files = sorted(core_root.rglob("*.py"))
    assert len(python_files) >= 30, (
        f"Floor check failed: expected >= 30 files, got {len(python_files)}"
    )

    all_violations: list[str] = []
    for py_file in python_files:
        tree = ast.parse(py_file.read_text(), str(py_file))
        all_violations.extend(_check_ast_clean(py_file, tree))

    assert not all_violations, "Persistence boundary violations in src/cliffracer:\n" + "\n".join(
        all_violations
    )


def test_core_public_api_exports_no_persistence_symbols():
    """Verify cliffracer public surface exports no persistence abstractions."""
    import cliffracer

    exported = getattr(cliffracer, "__all__", [])
    persistence_exports = [
        name
        for name in exported
        if BANNED_PERSISTENCE_CLASS_PATTERN.search(name)
        or BANNED_PERSISTENCE_FUNCTION_PATTERN.search(name)
    ]
    assert not persistence_exports, (
        f"cliffracer.__all__ exports persistence symbols: {persistence_exports}"
    )


def test_CONTROL_persistence_boundary_catches_violations():
    """Negative control: verify checker functions detect simulated persistence violations."""
    fake_pyproject = {
        "project": {
            "dependencies": ["nats-py>=2.9.0", "asyncpg>=0.29.0"],
            "optional-dependencies": {
                "db": ["sqlalchemy>=2.0.0"],
            },
        }
    }
    dep_violations = _check_dependencies_clean(fake_pyproject)
    assert len(dep_violations) == 2

    fake_code = (
        "import asyncpg\n"
        "from sqlalchemy.orm import Session\n"
        "class ConnectionPool:\n"
        "    pass\n"
        "def run_migrations():\n"
        "    pass\n"
    )
    fake_tree = ast.parse(fake_code, "fake_file.py")
    ast_violations = _check_ast_clean(Path("fake_file.py"), fake_tree)
    assert len(ast_violations) == 4


def _first_party_packages() -> list[Path]:
    return sorted(path.parent for path in (REPO / "packages").glob("*/pyproject.toml"))


def test_no_first_party_package_depends_on_a_persistence_library():
    """The boundary holds for every distribution in the repository, not only for core."""
    packages = _first_party_packages()
    assert len(packages) >= 5, packages

    violations = [
        f"{package.name}: {violation}"
        for package in packages
        for violation in _check_dependencies_clean(
            tomllib.loads((package / "pyproject.toml").read_text())
        )
    ]

    assert not violations, violations


def test_no_first_party_package_imports_a_persistence_library():
    sources = [
        path for package in _first_party_packages() for path in (package / "src").rglob("*.py")
    ]
    assert len(sources) >= 30, len(sources)

    imports: list[str] = []
    for path in sources:
        for node in ast.walk(ast.parse(path.read_text(), str(path))):
            names = (
                [alias.name for alias in node.names]
                if isinstance(node, ast.Import)
                else [node.module]
                if isinstance(node, ast.ImportFrom) and node.module
                else []
            )
            imports += [
                f"{path.relative_to(REPO)}:{node.lineno} imports {name}"
                for name in names
                if name.split(".")[0].lower() in BANNED_PERSISTENCE_MODULES
            ]

    assert not imports, imports


def test_CONTROL_a_package_that_declared_a_driver_would_be_found():
    fake = {"project": {"dependencies": ["cliffracer", "asyncpg>=0.29"]}}

    assert _check_dependencies_clean(fake) == ["dependencies: asyncpg>=0.29"]
