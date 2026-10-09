"""The core library reaches no web stack, at import time or at call time.

Two tiers, because one does not cover the other. The subprocess check imports
every core submodule and reads sys.modules, which catches a module-level import
including one in a submodule nothing imports eagerly. The AST sweep reads every
core source file and rejects a web-stack import at any nesting depth, which is
what catches an import written inside a function body -- invisible to the first
tier by construction, since the function need never run.
"""

import ast
import json
import os
import re
import subprocess
import sys
import tomllib
from pathlib import Path

import pytest

import cliffracer

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

#: The `src` directory this process imported `cliffracer` from. A child given its own `PYTHONPATH`
#: is pointed at it too, so it checks the code under test and not whatever the interpreter has
#: installed.
IMPORTED_SRC = str(Path(cliffracer.__file__).resolve().parents[1])

_CHECK = (
    "import importlib, pkgutil, sys, cliffracer; "
    "mods = [m.name for m in pkgutil.walk_packages(cliffracer.__path__, 'cliffracer.') "
    "if not m.name.endswith('.__main__')]; "
    "[importlib.import_module(m) for m in mods]; "
    "web = sorted({m.split('.')[0] for m in sys.modules "
    "if m.startswith(('fastapi', 'uvicorn', 'starlette'))}); "
    "print(f'{len(mods)}:' + ','.join(web))"
)


def test_importing_cliffracer_pulls_in_no_web_stack():
    """Verify all submodules in core package pull in no web stack modules."""
    out = subprocess.run(
        [sys.executable, "-c", _CHECK],
        capture_output=True,
        text=True,
        check=True,
    )
    count_str, _, web_str = out.stdout.strip().partition(":")
    # Compared against the modules on disk rather than a hand-set floor: a
    # packaging change that stopped shipping a subpackage would sail over a
    # floor while sweeping less than it claims.
    on_disk = {f for f in (REPO / "src" / "cliffracer").rglob("*.py") if f.name != "__main__.py"}
    expected = (
        len(
            {f for f in on_disk if f.name != "__init__.py"}
            | {f.parent for f in on_disk if f.name == "__init__.py"}
        )
        - 1
    )
    assert int(count_str) >= expected, (
        f"walk_packages swept {count_str} submodules but {expected} are on disk; "
        "the sweep is narrower than the package"
    )
    imported = [m for m in web_str.split(",") if m]
    assert imported == [], (
        f"importing cliffracer submodules pulled in web stack modules: {imported}"
    )


WEB_STACK = ("fastapi", "uvicorn", "starlette", "aiohttp", "httpx", "http.server")


def web_stack_imports(source: str, where: str) -> list[str]:
    """Return every web-stack import in `source`, at any nesting depth.

    ast.walk descends into function and class bodies, so an import written
    inside `def start()` is reported exactly like a module-level one.
    """
    found: list[str] = []
    for node in ast.walk(ast.parse(source, filename=where)):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith(WEB_STACK):
                    found.append(f"{where}:{node.lineno} imports {alias.name}")
        elif isinstance(node, ast.ImportFrom) and node.module:
            if node.module.startswith(WEB_STACK):
                found.append(f"{where}:{node.lineno} imports from {node.module}")
    return found


def test_no_core_source_file_imports_a_web_stack_module():
    """No file under src/cliffracer names a web framework in any import.

    The subprocess check above cannot see a function-local import: the import
    only happens when the function is called, and importing the module does not
    call it. This reads the source instead, so nesting does not hide it.
    """
    core_root = REPO / "src" / "cliffracer"
    sources = sorted(core_root.rglob("*.py"))
    assert len(sources) >= 20, f"only {len(sources)} core source files found; the sweep is broken"

    offenders: list[str] = []
    for py in sources:
        offenders.extend(web_stack_imports(py.read_text(), str(py.relative_to(REPO))))

    assert not offenders, (
        "the core library must reach no web stack; these import one:\n  " + "\n  ".join(offenders)
    )


def test_CONTROL_a_function_local_web_import_is_reported():
    """Control: the sweep sees an import the runtime check cannot.

    This is the exact shape that passes the sys.modules probe -- a lazy import
    inside a method body that nothing calls at import time.
    """
    source = (
        "class HealthListener:\n"
        "    async def start(self):\n"
        "        import fastapi\n"
        "        import uvicorn\n"
        "        self._app = fastapi.FastAPI()\n"
    )
    found = web_stack_imports(source, "sample.py")
    assert len(found) == 2, found
    assert any("imports fastapi" in f for f in found), found
    assert any("imports uvicorn" in f for f in found), found


def test_CONTROL_an_ordinary_module_reports_nothing():
    """Control: the sweep is not simply reporting every import it sees."""
    source = "import asyncio\nfrom pathlib import Path\n\n\ndef go():\n    import json\n    return json, asyncio, Path\n"
    assert web_stack_imports(source, "sample.py") == []


def test_CONTROL_the_check_can_see_the_web_stack_when_it_is_imported(tmp_path: Path):
    """Verify check detects web stack modules when explicitly imported.

    The check reads module names out of `sys.modules`, so a stand-in package
    named `fastapi` is what it has to see. It is a stand-in on the subprocess's
    path, first in line, because nothing in this workspace installs the real
    framework.
    """
    stand_in = tmp_path / "fastapi"
    stand_in.mkdir()
    (stand_in / "__init__.py").write_text("")
    out = subprocess.run(
        [sys.executable, "-c", "import fastapi; " + _CHECK],
        capture_output=True,
        text=True,
        check=True,
        env=_stand_in_env(tmp_path),
    )
    _, _, web_str = out.stdout.strip().partition(":")
    assert "fastapi" in web_str, out.stdout


def _stand_in_env(stand_in_dir: Path) -> dict[str, str]:
    """The CONTROL's environment: the stand-in's directory first, then the imported `src`."""
    return {**os.environ, "PYTHONPATH": os.pathsep.join([str(stand_in_dir), IMPORTED_SRC])}


def test_the_controls_child_imports_the_cliffracer_this_process_imported(tmp_path: Path):
    done = subprocess.run(
        [sys.executable, "-c", "import cliffracer, sys; sys.stdout.write(cliffracer.__file__)"],
        capture_output=True,
        text=True,
        env=_stand_in_env(tmp_path),
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert Path(done.stdout).resolve().parents[1] == Path(IMPORTED_SRC), (done.stdout, IMPORTED_SRC)


# The probe below runs in a subprocess with the web stack made unimportable, so
# the listener answers with the standard library or not at all. It is the
# runtime half of the static sweep above: the sweep proves no source file names
# a web framework, this proves none is reached at request time either.
_BLOCKED_PROBE = """
import asyncio, json, sys, urllib.request

for name in {web_stack!r}:
    sys.modules[name] = None          # any import of these now raises ImportError

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.health_listener import HealthListener


async def main() -> None:
    # health_port=0 asks the operating system for a free port. The default is
    # 8000, and this probe runs on a shared host: anything else holding 8000
    # made this test fail with a bind error that named a port nobody here asked
    # for. The constructor's port below cannot supply this -- start() re-reads
    # config.health_port whenever the service has a config, which is asserted
    # in tests/unit/test_health_listener.py.
    service = CliffracerService(ServiceConfig(name="probe", health_port=0))
    status = {{"value": "healthy"}}

    async def health_check():
        return {{"status": status["value"]}}

    service.health_check = health_check
    listener = HealthListener(service, "127.0.0.1", 0)
    await listener.start()

    def get(path):
        url = f"http://127.0.0.1:{{listener.port}}{{path}}"
        try:
            with urllib.request.urlopen(url) as response:
                return response.status
        except urllib.error.HTTPError as exc:
            return exc.code

    healthy = await asyncio.to_thread(get, "/health")
    status["value"] = "degraded"
    degraded = await asyncio.to_thread(get, "/health")
    port = listener.port
    await listener.stop()
    print(json.dumps({{"healthy": healthy, "degraded": degraded, "port": port}}))


asyncio.run(main())
"""


def test_the_health_listener_answers_with_the_web_stack_unimportable():
    """The probes are served by the standard library, not by a web framework.

    `sys.modules[name] = None` makes `import name` raise, so a listener that
    reached for fastapi or uvicorn at request time would fail rather than fall
    back. Both states are asked for: a 200 alone would pass on a listener that
    answers 200 to everything.
    """
    # Imported here rather than at module scope: this module deliberately keeps
    # its own import surface small, and the default is read from the field so
    # that raising it cannot leave the check below asserting a number the code
    # no longer uses.
    from cliffracer import ServiceConfig

    health_port_default = ServiceConfig.model_fields["health_port"].default

    probe = _BLOCKED_PROBE.format(web_stack=WEB_STACK)
    result = subprocess.run(
        [sys.executable, "-c", probe],
        capture_output=True,
        text=True,
        cwd=REPO,
        timeout=60,
    )
    assert result.returncode == 0, f"the probe did not finish:\n{result.stdout}\n{result.stderr}"
    reported = json.loads(result.stdout.strip().splitlines()[-1])
    port = reported.pop("port")
    assert reported == {"healthy": 200, "degraded": 503}, (
        f"the listener answered {reported} with the web stack blocked; "
        "it must serve 200 when healthy and 503 when not"
    )
    # The probe must not land on the default 8000. It ran as a subprocess on a
    # shared host, so a fixed port is a collision with whatever else is up: the
    # failure is `[errno 98] address already in use` naming 8000, attributed to
    # whichever branch happened to be under it. Asserted on the port the
    # listener actually bound rather than on the source of the probe above,
    # because the config field and the constructor argument disagree about
    # which one decides and only the socket knows the answer.
    assert port != health_port_default, (
        f"the probe bound the default port {health_port_default}, so it fails "
        "whenever anything else on the host holds it; ask for 0 instead"
    )


# The distributions the core library may require at runtime, written out rather
# than read from pyproject.toml: a check that compares the file to itself
# passes whatever the file becomes, and the point of this one is that adding a
# runtime dependency to the core library is a deliberate act.
CORE_RUNTIME_DISTRIBUTIONS = {"nats-py", "pydantic", "loguru"}

_REQUIREMENT_NAME = re.compile(r"^[A-Za-z0-9._-]+")


def declared_core_dependencies() -> dict[str, str]:
    """Map distribution name to the full requirement string, from pyproject."""
    data = tomllib.loads((REPO / "pyproject.toml").read_text())
    declared = {}
    for requirement in data["project"]["dependencies"]:
        name = _REQUIREMENT_NAME.match(requirement)
        assert name, f"cannot read a distribution name from {requirement!r}"
        declared[name.group(0)] = requirement
    return declared


def test_the_core_library_requires_only_the_declared_distributions():
    """The runtime dependency list is what an installer acts on.

    The sweeps above read the source. Neither would notice a web framework
    added to `[project].dependencies`, which is how it would arrive in every
    install of the library whether or not any module imports it yet.
    """
    declared = declared_core_dependencies()
    assert set(declared) == CORE_RUNTIME_DISTRIBUTIONS, (
        f"core runtime dependencies are {sorted(declared)}; this test names "
        f"{sorted(CORE_RUNTIME_DISTRIBUTIONS)}. Adding one is a deliberate act: "
        "change both, or move the dependency to an optional extra."
    )


def test_no_declared_core_dependency_is_a_web_framework():
    """Said separately from the set above, because it is the rule that matters.

    A future edit that updates both the list and this test's literal set in one
    go would keep the check above green. This one cannot be satisfied that way.
    """
    offenders = [
        requirement
        for name, requirement in declared_core_dependencies().items()
        if name.lower().startswith(WEB_STACK)
    ]
    assert not offenders, f"web frameworks declared as core runtime dependencies: {offenders}"


def test_every_core_dependency_states_a_version_range():
    """An unbounded requirement lets a major version arrive without a decision."""
    unbounded = [
        requirement
        for requirement in declared_core_dependencies().values()
        if not any(op in requirement for op in (">=", "==", "~=", "<"))
    ]
    assert not unbounded, f"these declare no version constraint: {unbounded}"


def test_the_checks_child_inherits_the_cliffracer_this_process_imported():
    """`test_importing_cliffracer_pulls_in_no_web_stack` passes its child no environment of its own,
    so the child imports cliffracer from where this process did only while that is reachable from
    the environment (the interpreter's install or `PYTHONPATH`), not from `sys.path` alone."""
    done = subprocess.run(
        [sys.executable, "-c", "import cliffracer, sys; sys.stdout.write(cliffracer.__file__)"],
        capture_output=True,
        text=True,
    )

    assert done.returncode == 0, done.stdout + done.stderr
    assert Path(done.stdout).resolve().parents[1] == Path(IMPORTED_SRC), (done.stdout, IMPORTED_SRC)
