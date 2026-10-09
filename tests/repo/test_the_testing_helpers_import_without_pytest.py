"""`cliffracer.testing` imports without pytest installed.

It is part of the shipped package, so a user imports it to build their own
tests -- and pytest is a development dependency of this project, not a runtime
one. A top-level `import pytest` anywhere under that namespace turns
`import cliffracer.testing` into a ModuleNotFoundError for anyone who has not
installed it.

That happened: `host_load.skip_if_the_host_is_too_busy_to_judge` needs
`pytest.skip`, and importing pytest at module scope broke the whole namespace.
The import moved into the function. This asserts it stays there, since the
mistake is invisible in an environment that has pytest -- which is every
environment this suite runs in.
"""

from __future__ import annotations

import subprocess
import sys

import pytest

pytestmark = pytest.mark.repo

# Block pytest, then import. `sys.modules[name] = None` makes the import
# machinery raise rather than fall through to the installed copy.
PROBE = """
import sys

sys.modules["pytest"] = None
import cliffracer.testing

print("imported", len(cliffracer.testing.__all__), "names")
"""


def test_the_testing_namespace_imports_with_pytest_unavailable():
    result = subprocess.run(
        [sys.executable, "-c", PROBE], capture_output=True, text=True, check=False
    )

    assert result.returncode == 0, (
        "`import cliffracer.testing` needs pytest, so a user without it cannot "
        f"use the shipped test helpers:\n{result.stdout}\n{result.stderr}"
    )
    assert "imported" in result.stdout, result.stdout


def test_CONTROL_the_probe_really_blocks_pytest():
    """Otherwise the test above passes because the block does nothing."""
    blocked = PROBE.replace("import cliffracer.testing", "import pytest")

    result = subprocess.run(
        [sys.executable, "-c", blocked], capture_output=True, text=True, check=False
    )

    assert result.returncode != 0, (
        "importing pytest succeeded under the block, so the test above says "
        f"nothing about pytest being unavailable:\n{result.stdout}"
    )
    assert "pytest" in result.stderr, result.stderr
