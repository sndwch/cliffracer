"""Nothing in the library or its packages dials a broker with `nats.connect`: the framework's dial is the one way.

`cliffracer.core.dial.connect` bounds a dial by a wall-clock time and closes the client a cut-off
leaves behind, which `nats.connect` cannot do because it builds its client inside the call. A
package that calls `nats.connect` leaves that client, and the socket it opened, to the garbage
collector when its own timeout cuts the dial. This reads the core's source, the client generator
`cliffracer-generate-client` included, and each package's source, and fails on a call spelled
`nats.connect(...)`.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def _bare_dials(source: str, filename: str = "<source>") -> list[int]:
    return [
        node.lineno
        for node in ast.walk(ast.parse(source, filename=filename))
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "connect"
        and isinstance(node.func.value, ast.Name)
        and node.func.value.id == "nats"
    ]


SOURCES = [REPO / "src", *sorted((REPO / "packages").glob("*/src"))]


def test_nothing_calls_nats_connect():
    found = {
        f"{path.relative_to(REPO)}:{line}": None
        for root in SOURCES
        for path in sorted(root.rglob("*.py"))
        for line in _bare_dials(path.read_text(), str(path))
    }

    assert not found, f"a bare nats.connect: {sorted(found)}"


def test_CONTROL_the_scan_sees_a_bare_dial():
    assert _bare_dials("import nats\n\n\nasync def dial():\n    return await nats.connect(url)\n")


def test_CONTROL_the_scan_does_not_flag_the_frameworks_dial_or_a_method_named_connect():
    assert not _bare_dials(
        "from cliffracer.core import dial\n\n\nasync def go(pool):\n"
        "    await dial.connect(url, timeout=1)\n    await pool.connect()\n"
    )


def test_CONTROL_the_core_and_the_packages_are_found():
    assert list((REPO / "src").rglob("*.py")), "the scan reads no core files"
    assert list((REPO / "packages").glob("*/src/**/*.py")), "the scan reads no package files"
