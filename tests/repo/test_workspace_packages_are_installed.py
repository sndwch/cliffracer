"""Verify workspace member packages are installed.

Ensures all extension packages under `packages/` are installed in the
environment, providing clear diagnostic instructions if packages
are missing.
"""

import importlib
import importlib.metadata
import pathlib
import re
import tomllib

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


def workspace_distributions() -> list[str]:
    """Distribution names defined in packages/*/pyproject.toml."""
    dists = []
    for pyproject in sorted(ROOT.glob("packages/*/pyproject.toml")):
        data = tomllib.loads(pyproject.read_text())
        dists.append(data["project"]["name"])
    return dists


def missing_distributions(dists: list[str]) -> list[str]:
    """Identify distribution packages not installed in the environment metadata."""
    absent = []
    for dist in dists:
        try:
            importlib.metadata.distribution(dist)
        except importlib.metadata.PackageNotFoundError:
            absent.append(dist)
    return absent


def test_the_discovery_finds_every_package():
    """Without this, an empty glob would make the guard below vacuously green."""
    modules = workspace_modules()
    dists = [p for p in sorted(ROOT.glob("packages/*")) if (p / "pyproject.toml").exists()]
    assert len(modules) == len(dists), (
        f"{len(dists)} distributions, {len(modules)} modules: {modules}"
    )
    assert modules, "no workspace packages found; the layout moved and this guard is blind"


def test_every_workspace_package_is_installed():
    """Verify all workspace packages are registered as installed distributions.

    Checks distribution metadata in the virtual environment. Merely placing
    package directories on sys.path does not satisfy this check.
    """
    absent = missing_distributions(workspace_distributions())
    assert not absent, (
        "workspace packages are not installed in the environment: "
        + ", ".join(absent)
        + f"\n\nRun: {SYNC}\n\n"
        "`uv sync --extra dev` installs core only -- every extension under "
        "packages/ is a separate distribution. Without them the package tests "
        "cannot be collected and the documentation guard reports every "
        "`import cliffracer_*` in the docs as unresolvable."
    )


def test_every_workspace_package_is_importable():
    absent = missing(workspace_modules())
    assert not absent, (
        "workspace packages are not installed: " + ", ".join(absent) + f"\n\nRun: {SYNC}\n\n"
        "`uv sync --extra dev` installs core only -- every extension under "
        "packages/ is a separate distribution. Without them the package tests "
        "cannot be collected and the documentation guard reports every "
        "`import cliffracer_*` in the docs as unresolvable."
    )


def test_CONTROL_a_missing_distribution_is_detected():
    """Verify that uninstalled distributions are reported by missing_distributions."""
    assert missing_distributions(["cliffracer-nonexistent-package"]) == [
        "cliffracer-nonexistent-package"
    ]


def test_CONTROL_a_missing_module_is_detected():
    """The checker reports what is absent, so the test above can fail."""
    assert missing(["cliffracer_definitely_not_installed"]) == [
        "cliffracer_definitely_not_installed"
    ]


def dev_syncs(text: str) -> list[str]:
    """Each line that installs the development environment: `uv sync` with `--extra dev`."""
    return re.findall(r"^[^\n]*\buv sync\b[^\n]*--extra dev[^\n]*$", text, re.M)


def not_the_workspace_command(lines: list[str]) -> list[str]:
    return [line.strip() for line in lines if SYNC not in line]


def _workflow_run_lines(path) -> list[str]:
    """The lines of every `run:` step of a workflow; a comment or a name is not one."""
    import yaml

    workflow = yaml.safe_load(path.read_text())
    return [
        line
        for job in (workflow.get("jobs") or {}).values()
        for step in job.get("steps", [])
        for line in str(step.get("run", "")).splitlines()
    ]


def test_every_documented_dev_install_is_the_workspace_command():
    """Not a list of the documents that teach it: every tracked document that does.

    `uv sync --extra dev` installs core alone, which is the mistake this module's message exists
    to correct, so a document that says it is wrong wherever it is.
    """
    from tests.repo.test_docs_code_blocks_resolve import docs

    teaching = {}
    for doc in docs():
        lines = dev_syncs(doc.read_text())
        if lines:
            teaching[str(doc.relative_to(ROOT))] = lines

    assert {"README.md", "CONTRIBUTING.md", "CLAUDE.md", "examples/ecommerce/README.md"} <= set(
        teaching
    ), sorted(teaching)
    wrong = {rel: not_the_workspace_command(lines) for rel, lines in teaching.items()}
    assert not any(wrong.values()), {rel: bad for rel, bad in wrong.items() if bad}


def test_every_workflow_that_installs_the_dev_environment_runs_the_workspace_command():
    """Both CI definitions and the scheduled one, read from their steps rather than their text.

    A substring over the whole file is satisfied by a comment, or by a job other than the one
    installing for the test run.
    """
    workflows = sorted((ROOT / ".gitea" / "workflows").glob("*.yml")) + sorted(
        (ROOT / ".github" / "workflows").glob("*.yml")
    )
    installs = {
        str(path.relative_to(ROOT)): dev_syncs("\n".join(_workflow_run_lines(path)))
        for path in workflows
    }
    installs = {rel: lines for rel, lines in installs.items() if lines}

    assert {".gitea/workflows/ci.yml", ".github/workflows/ci.yml"} <= set(installs), sorted(
        installs
    )
    wrong = {rel: not_the_workspace_command(lines) for rel, lines in installs.items()}
    assert not any(wrong.values()), {rel: bad for rel, bad in wrong.items() if bad}


def test_CONTROL_a_dev_install_that_is_not_the_workspace_command_is_reported(tmp_path):
    assert not_the_workspace_command(dev_syncs("run: uv sync --extra dev\n")) == [
        "run: uv sync --extra dev"
    ]
    assert not_the_workspace_command(dev_syncs(f"run: {SYNC}\n")) == []
    # A release install is not the development one.
    assert dev_syncs("run: uv sync --locked --group release\n") == []


def test_CONTROL_a_comment_does_not_stand_in_for_the_install_step(tmp_path):
    """The workspace command in a comment, and the wrong one in the step that runs."""
    workflow = tmp_path / "ci.yml"
    workflow.write_text(
        f"# install with: {SYNC}\n"
        "jobs:\n  test:\n    steps:\n      - name: install\n"
        "        run: uv sync --extra dev\n"
    )

    assert not_the_workspace_command(dev_syncs("\n".join(_workflow_run_lines(workflow)))) == [
        "uv sync --extra dev"
    ]
