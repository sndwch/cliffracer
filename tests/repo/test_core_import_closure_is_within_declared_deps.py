"""Importing core pulls in nothing core does not declare, whatever else is installed.

ADR-0023 limits core's runtime dependencies to nats-py, pydantic and loguru. Two things enforce it:
`pyproject.toml` says what is declared (read by `test_cli_yaml_extra.py` and
`test_dependency_lists_agree.py`), and this reads what the code imports. The development
environment installs every extra (PyYAML, msgpack, httpx, croniter, ...), so an unconditional
`import msgpack` or `import yaml` at the top of a core module imports cleanly here and makes
`pip install cliffracer` unimportable. The web-stack guard covers three names; this covers every
installed distribution core does not declare.

How: a subprocess installs an import hook that refuses any installed third-party top-level module
outside the transitive closure of core's declared dependencies (the standard library and `cliffracer`
itself are always allowed), then imports every `cliffracer.*` module. A module that imports an
undeclared distribution unconditionally fails to import, and is named with the module it asked for.
An optional import guarded by `try/except ImportError` is untouched: the hook raises
`ModuleNotFoundError`, which is what an absent package raises.

What it cannot see: an undeclared import written inside a function body, which only runs when
called. The AST sweep in `test_core_imports_no_web_stack.py` reads those for the web stack.
"""

import json
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

_PROBE = textwrap.dedent(
    """
    import importlib, importlib.abc, importlib.metadata as md, json, pkgutil, re, sys, tomllib

    pyproject, src = sys.argv[1], sys.argv[2]

    def canon(name):
        return re.sub(r"[-_.]+", "-", name).lower()

    declared = tomllib.load(open(pyproject, "rb"))["project"]["dependencies"]
    roots = [canon(re.split(r"[ ;<>=!~\\[(]", d)[0]) for d in declared]
    providers = md.packages_distributions()  # top-level module -> distributions

    closure, stack = set(), list(roots)
    while stack:
        name = stack.pop()
        if name in closure:
            continue
        closure.add(name)
        try:
            requirements = md.requires(name) or []
        except md.PackageNotFoundError:
            continue
        for requirement in requirements:
            if "extra ==" in requirement or "extra==" in requirement:
                continue
            stack.append(canon(re.split(r"[ ;<>=!~\\[(]", requirement)[0]))

    allowed = {top for top, dists in providers.items() if any(canon(d) in closure for d in dists)}
    allowed |= set(sys.stdlib_module_names) | {"cliffracer"}
    refused = {}

    class Refuse(importlib.abc.MetaPathFinder):
        def find_spec(self, fullname, path=None, target=None):
            top = fullname.split(".")[0]
            if top in providers and top not in allowed:
                refused.setdefault(top, set()).add(fullname)
                raise ModuleNotFoundError(f"{top} is not a declared core dependency", name=top)
            return None

    sys.meta_path.insert(0, Refuse())
    sys.path.insert(0, src)
    failed = {}
    try:
        import cliffracer
        modules = [
            m.name for m in pkgutil.walk_packages(cliffracer.__path__, "cliffracer.")
            if not m.name.endswith(".__main__")
        ]
    except Exception as exc:
        # `import cliffracer` itself failed: the undeclared import is reached from the package.
        failed["cliffracer"] = f"{type(exc).__name__}: {exc}"
        modules = []
    for name in modules:
        try:
            importlib.import_module(name)
        except Exception as exc:
            failed[name] = f"{type(exc).__name__}: {exc}"
    print(json.dumps({
        "modules": len(modules),
        "allowed_third_party": sorted(t for t in allowed if t in providers),
        "refused": {k: sorted(v) for k, v in refused.items()},
        "failed": failed,
    }))
    """
)


def import_every_core_module(src: Path, pyproject: Path = REPO / "pyproject.toml") -> dict:
    done = subprocess.run(
        [sys.executable, "-c", _PROBE, str(pyproject), str(src)],
        capture_output=True,
        text=True,
        timeout=180,
    )
    assert done.returncode == 0, done.stderr[-1500:]
    return json.loads(done.stdout.strip().splitlines()[-1])


def test_every_core_module_imports_with_only_the_declared_dependencies():
    report = import_every_core_module(REPO / "src")

    on_disk = {f for f in (REPO / "src" / "cliffracer").rglob("*.py") if f.name != "__main__.py"}
    expected = (
        len(
            {f for f in on_disk if f.name != "__init__.py"}
            | {f.parent for f in on_disk if f.name == "__init__.py"}
        )
        - 1
    )
    assert report["modules"] >= expected, (
        f"swept {report['modules']} modules but {expected} are on disk; the sweep is narrower"
    )
    assert {"nats", "pydantic", "loguru"} <= set(report["allowed_third_party"]), report
    assert report["failed"] == {}, (
        "core modules that cannot import with only the declared dependencies, with the "
        f"undeclared distribution each asked for: {report['refused']}; failures: {report['failed']}"
    )


# ---- controls: each way the sweep could pass wrongly ------------------------------------------


def _copy_of_core(tmp: Path) -> Path:
    dest = tmp / "src"
    shutil.copytree(REPO / "src" / "cliffracer", dest / "cliffracer")
    return dest


@pytest.mark.parametrize("module", ["msgpack", "yaml"])
def test_CONTROL_an_unconditional_import_of_an_installed_extra_is_caught(module):
    """The mutation the ticket ran by hand: the line goes in, every other test stays green."""
    pytest.importorskip(module)
    with tempfile.TemporaryDirectory() as tmp:
        src = _copy_of_core(Path(tmp))
        target = src / "cliffracer" / "core" / "service.py"
        target.write_text(target.read_text() + f"\nimport {module}\n")

        report = import_every_core_module(src)

    assert module in report["refused"], report["refused"]
    assert any(name.startswith("cliffracer") for name in report["failed"]), report["failed"]


def test_CONTROL_an_optional_import_guarded_by_try_except_is_not_a_failure():
    pytest.importorskip("msgpack")
    with tempfile.TemporaryDirectory() as tmp:
        src = _copy_of_core(Path(tmp))
        target = src / "cliffracer" / "core" / "service.py"
        target.write_text(
            target.read_text()
            + "\ntry:\n    import msgpack\nexcept ImportError:\n    msgpack = None\n"
        )

        report = import_every_core_module(src)

    assert "msgpack" in report["refused"], "the hook was asked and refused"
    assert report["failed"] == {}, report["failed"]


def test_CONTROL_the_declared_dependencies_and_the_standard_library_are_allowed():
    report = import_every_core_module(REPO / "src")

    assert {"nats", "pydantic", "pydantic_core", "loguru"} <= set(report["allowed_third_party"])
    assert "msgpack" not in report["allowed_third_party"]
    assert "yaml" not in report["allowed_third_party"]
