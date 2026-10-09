"""Every name a distribution imports from core or from another distribution is still there.

The set is read from the tree: each `from cliffracer... import name` and `from cliffracer_x
import name` in `packages/*/src` and `packages/*/tests`, wherever the statement sits in the file,
including inside a function and under `TYPE_CHECKING`. A hand-kept list of the cross-package
names held two entries, one of which no package imported, while the packages imported dozens.

A consumer that imports the name where it is used finds out when that code runs. A rename in core
then fails a distribution's own tests, or an installation, instead of this one line. What is not
read: `import cliffracer.x` followed by an attribute access, a name reached through `getattr`,
and `from ... import *`.
"""

import ast
import importlib
from collections import defaultdict
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
PACKAGES = REPO / "packages"


def imported_across_distributions(packages: Path) -> dict[tuple[str, str], list[str]]:
    """Each (module, name) a distribution imports from another one, with the files that do.

    "Another one" is core (`cliffracer`) or a `cliffracer_*` package that is not the importing
    distribution's own. Relative imports and `*` are not names this can look up.
    """
    found: dict[tuple[str, str], list[str]] = defaultdict(list)
    for distribution in sorted(p for p in packages.iterdir() if (p / "src").is_dir()):
        own = {d.name for d in (distribution / "src").iterdir() if d.is_dir()}
        for part in ("src", "tests"):
            for path in sorted((distribution / part).rglob("*.py")):
                for node in ast.walk(ast.parse(path.read_text(), filename=str(path))):
                    if not isinstance(node, ast.ImportFrom) or node.level or not node.module:
                        continue
                    top = node.module.split(".")[0]
                    if top in own or not (top == "cliffracer" or top.startswith("cliffracer_")):
                        continue
                    for alias in node.names:
                        if alias.name != "*":
                            found[(node.module, alias.name)].append(
                                str(path.relative_to(packages.parent))
                            )
    return found


def unresolved(imports: dict[tuple[str, str], list[str]]) -> list[str]:
    """What cannot be imported, one line each, naming the files that need it."""
    missing = []
    for (module, name), files in sorted(imports.items()):
        try:
            mod = importlib.import_module(module)
        except ImportError as exc:
            missing.append(f"{module} cannot be imported ({exc}), needed by {files[0]}")
            continue
        if hasattr(mod, name):
            continue
        try:
            importlib.import_module(f"{module}.{name}")  # a submodule is importable by name
        except ImportError:
            missing.append(f"{module}.{name} is missing, needed by {', '.join(files[:3])}")
    return missing


def test_every_name_a_distribution_imports_from_another_stays_importable():
    imports = imported_across_distributions(PACKAGES)

    assert unresolved(imports) == []


def test_CONTROL_the_walk_reads_the_names_the_packages_import_today():
    """A walk that read nothing would pass the test above. Two names the old list held by hand,
    and a count that a package losing its imports would drop below."""
    imports = imported_across_distributions(PACKAGES)

    assert ("cliffracer.testing", "MockMessage") in imports
    assert any(
        f.startswith("packages/cliffracer-otel/")
        for f in imports[("cliffracer.testing", "MockMessage")]
    )
    assert len({module for module, _ in imports}) >= 10, sorted({m for m, _ in imports})
    assert len(imports) >= 40, len(imports)


def _write(root: Path, relative: str, source: str) -> None:
    path = root / relative
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(source)


def test_CONTROL_a_name_that_is_gone_is_reported_with_the_file_that_needs_it(tmp_path):
    """The check can fail: a name core does not have, imported at the top, inside a function,
    under TYPE_CHECKING and from a test, is reported once per name with its files."""
    _write(
        tmp_path,
        "packages/pkg-a/src/pkg_a/mod.py",
        "from cliffracer.core.exceptions import NoSuchName, ServiceLifecycleError\n"
        "def late():\n"
        "    from cliffracer.testing import AlsoGone\n",
    )
    _write(
        tmp_path,
        "packages/pkg-a/tests/test_mod.py",
        "from typing import TYPE_CHECKING\n"
        "if TYPE_CHECKING:\n"
        "    from cliffracer.core.exceptions import NoSuchName\n",
    )

    missing = unresolved(imported_across_distributions(tmp_path / "packages"))

    assert len(missing) == 2, missing
    assert any(
        "cliffracer.core.exceptions.NoSuchName" in m
        and "packages/pkg-a/src/pkg_a/mod.py" in m
        and "packages/pkg-a/tests/test_mod.py" in m
        for m in missing
    ), missing
    assert any("cliffracer.testing.AlsoGone" in m for m in missing), missing


def test_CONTROL_what_resolves_or_is_not_cross_package_is_not_reported(tmp_path):
    """A submodule named in the import, the package's own modules, relative imports, a star,
    another distribution's real name and a distribution's own
    `cliffracer_*` package all pass."""
    _write(
        tmp_path,
        "packages/pkg-a/src/pkg_a/mod.py",
        "from cliffracer.core import extension\n"
        "from cliffracer.core.exceptions import ServiceLifecycleError\n"
        "from pkg_a_other import whatever\n"
        "from cliffracer_auth import AuthExtension\n"
        "from . import sibling\n"
        "from cliffracer.core.exceptions import *\n",
    )
    _write(tmp_path, "packages/pkg-a/src/pkg_a/sibling.py", "from pkg_a.mod import x\n")
    _write(
        tmp_path,
        "packages/cliffracer-b/src/cliffracer_b/mod.py",
        "from cliffracer_b.other import NotThere\n",
    )

    imports = imported_across_distributions(tmp_path / "packages")

    assert set(imports) == {
        ("cliffracer.core", "extension"),
        ("cliffracer.core.exceptions", "ServiceLifecycleError"),
        ("cliffracer_auth", "AuthExtension"),
    }, sorted(imports)
    assert unresolved(imports) == []
