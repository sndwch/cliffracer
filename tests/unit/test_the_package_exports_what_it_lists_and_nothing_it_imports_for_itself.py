"""`cliffracer` and `cliffracer.generate_client` export what `__all__` lists.

`from importlib.metadata import PackageNotFoundError` left that name bound at the top of the
package, so `from cliffracer import PackageNotFoundError` resolved to an importlib exception that
has nothing to do with the framework. The generator's package published `annotation_text` and
`imports_for`, helpers whose defaults disagree with the one caller that counts (`emit`, which
always passes the aliases) and which nothing in the tree imports from the package.
"""

import subprocess
import sys
import types

import pytest

import cliffracer
from cliffracer import generate_client

pytestmark = pytest.mark.unit


def _public_names_not_listed(module: types.ModuleType) -> list[str]:
    return sorted(
        name
        for name, value in vars(module).items()
        if not name.startswith("_")
        and not isinstance(value, types.ModuleType)
        and name not in module.__all__
    )


def test_cliffracer_binds_no_public_name_that_its_all_does_not_list():
    assert _public_names_not_listed(cliffracer) == []


def test_every_name_cliffracer_lists_is_bound():
    assert [name for name in cliffracer.__all__ if not hasattr(cliffracer, name)] == []


def test_the_version_lookup_still_falls_back_when_the_package_is_not_installed():
    """In a fresh interpreter, because importing the package again here would rebind it."""
    code = (
        "import importlib.metadata as m\n"
        "def missing(name):\n"
        "    raise m.PackageNotFoundError(name)\n"
        "m.version = missing\n"
        "import cliffracer\n"
        "print(cliffracer.__version__)\n"
    )

    result = subprocess.run(
        [sys.executable, "-c", code], capture_output=True, text=True, timeout=60, check=False
    )

    assert (result.returncode, result.stdout.strip()) == (0, "0.0.0+unknown"), result.stderr


def test_the_generator_package_exports_the_emitter_and_its_error_only():
    assert generate_client.__all__ == ["CannotEmit", "emit"]
    assert not hasattr(generate_client, "annotation_text")
    assert not hasattr(generate_client, "imports_for")
    assert _public_names_not_listed(generate_client) == []
