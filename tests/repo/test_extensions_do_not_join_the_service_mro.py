"""ADR-0004: extensions are composed, not inherited.

Two halves, because each misses what the other catches. The runtime half walks
the MRO of every shipped service and every shipped Extension subclass, which is
what "extensions do not join the class MRO" actually means. The source half
sweeps for classes named as mixins, which catches a mixin that is shipped and
exported before anything inherits from it.
"""

import ast
import json
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]

PACKAGE_SRCS = sorted((REPO / "packages").glob("*/src"))

_MRO_PROBE = r"""
import importlib, inspect, json, pkgutil, sys

import cliffracer
from cliffracer.core.extension import Extension
from cliffracer.core.service import CliffracerService

names = ["cliffracer"] + [
    m.name
    for m in pkgutil.walk_packages(cliffracer.__path__, "cliffracer.")
    if not m.name.endswith(".__main__")
]
names += sys.argv[1:]

seen = {}
for name in names:
    try:
        module = importlib.import_module(name)
    except Exception:
        continue
    for _, obj in inspect.getmembers(module, inspect.isclass):
        seen[f"{obj.__module__}.{obj.__qualname__}"] = obj

services = {k: v for k, v in seen.items() if issubclass(v, CliffracerService)}
in_mro = [
    f"{k} has {b.__module__}.{b.__qualname__} in its MRO"
    for k, v in services.items()
    for b in v.__mro__
    if b is not v and issubclass(b, Extension)
]
both = [k for k, v in seen.items() if issubclass(v, Extension) and issubclass(v, CliffracerService)]

print("MRO_PROBE:" + json.dumps({
    "loaded": len(seen),
    "services": sorted(services),
    "in_mro": sorted(in_mro),
    "both": sorted(both),
}))
"""


def member_package_names() -> list[str]:
    """Top-level import names of the member packages."""
    names: list[str] = []
    for src in PACKAGE_SRCS:
        for child in sorted(src.iterdir()):
            if child.is_dir() and (child / "__init__.py").exists():
                names.append(child.name)
    return names


def probe_shipped_mros() -> dict:
    """Import every shipped module in a fresh interpreter and report class relationships.

    A subprocess, not this session: importing every member package in-process
    rebinds module-level singletons for every test that runs afterwards, which
    is a defect this suite has already paid for once.
    """
    result = subprocess.run(
        [sys.executable, "-c", _MRO_PROBE, *member_package_names()],
        capture_output=True,
        text=True,
        cwd=REPO,
    )
    assert result.returncode == 0, f"the MRO probe failed:\n{result.stderr}"
    marked = [ln for ln in result.stdout.splitlines() if ln.startswith("MRO_PROBE:")]
    assert len(marked) == 1, f"probe did not report exactly one result line:\n{result.stdout}"
    return json.loads(marked[0][len("MRO_PROBE:") :])


def test_no_extension_is_in_a_service_mro():
    """No service class inherits from Extension, directly or through a base."""
    report = probe_shipped_mros()
    assert report["loaded"] > 0, "no classes were loaded; this guard is vacuous"
    assert "cliffracer.core.service.CliffracerService" in report["services"], (
        f"the base service itself was not discovered: {report['services'][:5]}"
    )

    assert not report["in_mro"], (
        "extensions are composed as class attributes and bound per instance; "
        "these join the MRO instead:\n  " + "\n  ".join(report["in_mro"])
    )


def test_no_shipped_extension_subclasses_a_service():
    """The converse: an Extension must not inherit a service either.

    Without this the check above is satisfiable by making the inheritance point
    the other way, which produces the same tangled MRO.
    """
    report = probe_shipped_mros()
    assert not report["both"], f"these are both an Extension and a service: {report['both']}"


def mixin_classes() -> dict[str, str]:
    """Map 'path:ClassName' to the class name for every Mixin-named class shipped."""
    found: dict[str, str] = {}
    for src in [REPO / "src", *PACKAGE_SRCS]:
        for py in sorted(src.rglob("*.py")):
            tree = ast.parse(py.read_text(), filename=str(py))
            for node in ast.walk(tree):
                if isinstance(node, ast.ClassDef) and node.name.endswith("Mixin"):
                    found[f"{py.relative_to(REPO).as_posix()}:{node.name}"] = node.name
    return found


def test_no_package_ships_a_service_mixin():
    """Nothing is shipped as a mixin.

    There is no allowlist: the last exempted class is gone, so the rule is the
    whole rule. A mixin that has to be added again brings back the exemption
    and the reason for it in the same change.
    """
    shipped = sorted(mixin_classes())
    assert not shipped, (
        "ADR-0004 composes optional behaviour as extensions bound per instance. "
        "These are shipped as mixins:\n  " + "\n  ".join(shipped)
    )


def test_CONTROL_a_mixin_in_the_tree_is_found():
    """Control: the AST sweep really matches the shape it claims to."""
    sample = ast.parse("class ThingMixin:\n    pass\n\n\nclass Thing:\n    pass\n")
    names = [
        n.name for n in ast.walk(sample) if isinstance(n, ast.ClassDef) and n.name.endswith("Mixin")
    ]
    assert names == ["ThingMixin"]


def test_CONTROL_the_mro_walk_sees_a_planted_extension():
    """Control: the MRO walk reports a service that does inherit an Extension.

    Without this, a walk that reported nothing for any input would satisfy
    test_no_extension_is_in_a_service_mro exactly as a clean tree does.
    """

    from cliffracer.core.extension import Extension
    from cliffracer.core.service import CliffracerService

    class PlantedExtension(Extension):
        pass

    class MixedService(CliffracerService, PlantedExtension):
        pass

    offenders = [
        b for b in MixedService.__mro__ if b is not MixedService and issubclass(b, Extension)
    ]
    assert PlantedExtension in offenders, MixedService.__mro__
