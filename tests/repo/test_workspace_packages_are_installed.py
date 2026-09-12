"""Verify workspace member packages are installed.

Ensures all extension packages under `packages/` are installed in the
environment, providing clear diagnostic instructions if packages
are missing.
"""

import importlib
import pathlib

import pytest

pytestmark = pytest.mark.repo


ROOT = pathlib.Path(__file__).resolve().parents[2]
SYNC = "uv sync --all-packages --extra dev"


def workspace_modules() -> list[str]:
    """The top-level import name of every distribution under `packages/`.

    Derived from the `src/` directory contents rather than the package
    directory name, since distribution names (hyphenated) often differ from
    importable module names (underscored).
    """
    found = []
    for src in sorted(ROOT.glob("packages/*/src/*")):
        if src.is_dir() and (src / "__init__.py").exists():
            found.append(src.name)
    return found


def missing(modules: list[str]) -> list[str]:
    out = []
    for name in modules:
        try:
            importlib.import_module(name)
        except ImportError:
            out.append(name)
    return out


def test_the_discovery_finds_every_package():
    """Without this, an empty glob would make the guard below vacuously green."""
    modules = workspace_modules()
    dists = [p for p in sorted(ROOT.glob("packages/*")) if (p / "pyproject.toml").exists()]
    assert len(modules) == len(dists), (
        f"{len(dists)} distributions, {len(modules)} modules: {modules}"
    )
    assert modules, "no workspace packages found; the layout moved and this guard is blind"


def test_every_workspace_package_is_importable():
    absent = missing(workspace_modules())
    assert not absent, (
        "workspace packages are not installed: " + ", ".join(absent) + f"\n\nRun: {SYNC}\n\n"
        "`uv sync --extra dev` installs core only -- every extension under "
        "packages/ is a separate distribution. Without them the package tests "
        "cannot be collected and the documentation guard reports every "
        "`import cliffracer_*` in the docs as unresolvable."
    )


def test_CONTROL_a_missing_module_is_detected():
    """The checker reports what is absent, so the test above can fail."""
    assert missing(["cliffracer_definitely_not_installed"]) == [
        "cliffracer_definitely_not_installed"
    ]


def test_CONTROL_the_command_in_the_message_is_the_one_ci_runs():
    """A fix-it message that names a command nobody else runs goes stale.

    ci.yml's install step is what the repo actually runs, so the message and
    the command are pinned to each other rather than to a copy of the string.
    """
    ci = (ROOT / ".gitea" / "workflows" / "ci.yml").read_text()
    assert SYNC in ci, f"{SYNC!r} is not in ci.yml; one of the two moved"


# Documents that teach the development sync command.
TEACHING_DOCS = ("README.md", "examples/ecommerce/README.md")


def test_CONTROL_the_documents_that_teach_it_still_say_it():
    """Verify teaching documentation includes the workspace sync command."""
    wrong = [rel for rel in TEACHING_DOCS if SYNC not in (ROOT / rel).read_text()]
    assert not wrong, f"these no longer document {SYNC!r}: {wrong}"
