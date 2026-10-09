"""The `nats_required` marker is the suite's only handle on the broker tests.

`NATS_SKIP_INTEGRATION=true`, the no-broker skip, and the count in the terminal
summary all key on that marker alone. A test that dials a broker without it
runs anyway when the operator asked for the opposite, and is absent from the
count that would otherwise say so -- which is what this sweep exists to catch.
"""

import ast
import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
TEST_BROKER_URL_ENV = "CLIFFRACER_TEST_NATS_URL"
_PLUGIN = """
import json
import os


def pytest_collection_finish(session):
    with open(os.environ["CLIFFRACER_MARKER_NAMES_OUT"], "w") as out:
        json.dump(
            {item.nodeid: sorted({m.name for m in item.iter_markers()}) for item in session.items},
            out,
        )
"""


def _marker_names(tmp_path: Path) -> dict[str, set[str]]:
    """The marker names of every test pytest collects under tests/ and packages/.

    Collection is the deciding step -- a marker can come from a decorator, a
    module-level `pytestmark`, or a hook -- so this asks pytest rather than
    reading the files and guessing. `-m` selects on these same names, each
    item's `iter_markers()`, so one collection answers every marker expression
    below; a small plugin writes them out when collection finishes.
    """
    (tmp_path / "_marker_names_plugin.py").write_text(_PLUGIN)
    out = tmp_path / "marker_names.json"
    env = {k: v for k, v in os.environ.items() if k != TEST_BROKER_URL_ENV}
    env["PYTHONPATH"] = os.pathsep.join(filter(None, [str(tmp_path), env.get("PYTHONPATH")]))
    env["CLIFFRACER_MARKER_NAMES_OUT"] = str(out)
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/",
            "packages/",
            "--collect-only",
            "-q",
            "-p",
            "no:cacheprovider",
            "-p",
            "_marker_names_plugin",
        ],
        cwd=REPO,
        env=env,
        capture_output=True,
        text=True,
    )
    # 0 = tests collected. Anything else is a broken run, and reading markers
    # out of it would report "nothing missing" for the wrong reason.
    assert proc.returncode == 0, (
        f"collection failed ({proc.returncode}):\n{(proc.stdout + proc.stderr)[-2000:]}"
    )
    return {node: set(names) for node, names in json.loads(out.read_text()).items()}


def test_no_integration_test_is_missing_the_nats_required_marker(tmp_path):
    names = _marker_names(tmp_path)

    # CONTROL: a renamed or misspelled marker would select nothing below and
    # pass for the wrong reason -- the all-zero report the marker was meant to stop.
    assert any({"integration", "nats_required"} <= marks for marks in names.values()), (
        "no collected test is marked both integration and nats_required, so the "
        "check below cannot find anything"
    )
    missing = sorted(
        node
        for node, marks in names.items()
        if "integration" in marks and "nats_required" not in marks
    )
    assert not missing, (
        "these integration tests carry no nats_required marker, so the "
        "off-switch, the no-broker skip and the summary count all miss "
        "them:\n" + "\n".join(missing)
    )


# ---------------------------------------------------------------------------
# A test that dials a broker directly, whatever else it is marked
# ---------------------------------------------------------------------------
#
# The sweep above keys on the `integration` marker, so a test marked only `unit`
# that opens a connection to a broker is outside both of its selections. This
# reads the test sources instead. It sees DIRECT dials only: a `nats.connect(...)`
# call. A test that reaches the broker through a service it starts, or a client
# it builds, is not seen here, and nothing in this file can see it.
#
# The address is the line. A `nats.connect` whose address is a string literal
# names one place and is not the suite's broker: `test_first_connect_bounds.py`
# dials `nats://127.0.0.1:1` on purpose, a port nothing listens on, and belongs
# in the no-broker tier. Any other address -- `broker_url()`, a fixture's
# `nats_url`, a name bound to one of those -- or none at all, which means the
# default broker, is treated as a live one.

_MARKER = "nats_required"


def _test_sources() -> list[Path]:
    roots = [REPO / "tests", *sorted((REPO / "packages").glob("*/tests"))]
    return sorted(path for root in roots for path in root.rglob("test_*.py"))


def _mark_names(node: ast.AST) -> set[str]:
    """The `pytest.mark.<name>` names written anywhere inside *node*."""
    return {
        child.attr
        for child in ast.walk(node)
        if isinstance(child, ast.Attribute) and ast.unparse(child.value) == "pytest.mark"
    }


def _names_bound_to_a_literal(scope: ast.AST) -> set[str]:
    return {
        target.id
        for node in ast.walk(scope)
        if isinstance(node, ast.Assign)
        and isinstance(node.value, ast.Constant)
        and isinstance(node.value.value, str)
        for target in node.targets
        if isinstance(target, ast.Name)
    }


def _is_a_literal_address(node: ast.expr, literal_names: set[str]) -> bool:
    if isinstance(node, ast.Constant):
        return isinstance(node.value, str)
    return isinstance(node, ast.Name) and node.id in literal_names


def _dials_a_live_broker(test: ast.AST, module_literals: set[str]) -> bool:
    literals = module_literals | _names_bound_to_a_literal(test)
    for call in ast.walk(test):
        if not (isinstance(call, ast.Call) and isinstance(call.func, ast.Attribute)):
            continue
        if call.func.attr != "connect" or ast.unparse(call.func.value) != "nats":
            continue
        address = call.args[0] if call.args else None
        for keyword in call.keywords:
            if keyword.arg == "servers":
                address = keyword.value
        if address is None or not _is_a_literal_address(address, literals):
            return True
    return False


def live_broker_dials(source: str, filename: str = "<source>") -> list[tuple[str, bool]]:
    """Each test in *source* that dials a live broker directly, as
    (``file:line name``, whether it carries the `nats_required` marker).

    The marker counts from the test's own decorators, its class's, or the
    module's `pytestmark`, which is every place pytest takes it from here. A hook
    that added it would not be seen; neither conftest adds any.
    """
    tree = ast.parse(source, filename=filename)
    module_marks: set[str] = set()
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == "pytestmark" for t in node.targets
        ):
            module_marks |= _mark_names(node.value)
    module_literals = _names_bound_to_a_literal(
        ast.Module([n for n in tree.body if isinstance(n, ast.Assign)], [])
    )

    found: list[tuple[str, bool]] = []

    def visit(body: list[ast.stmt], inherited: set[str]) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit(node.body, inherited | _mark_names(ast.Module(node.decorator_list, [])))
            elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef) and node.name.startswith(
                "test"
            ):
                if _dials_a_live_broker(node, module_literals):
                    marks = inherited | _mark_names(ast.Module(node.decorator_list, []))
                    found.append((f"{filename}:{node.lineno} {node.name}", _MARKER in marks))

    visit(tree.body, module_marks)
    return found


def _all_dials() -> list[tuple[str, bool]]:
    return [
        dial
        for path in _test_sources()
        for dial in live_broker_dials(path.read_text(), str(path.relative_to(REPO)))
    ]


def test_no_test_dials_a_live_broker_without_the_nats_required_marker():
    unmarked = [where for where, marked in _all_dials() if not marked]

    assert not unmarked, (
        "these tests call nats.connect with an address that is not a string literal "
        'and carry no nats_required marker, so `-m "not nats_required"` runs them '
        "against whatever answers on that address:\n  " + "\n  ".join(unmarked)
    )


def test_CONTROL_the_sweep_finds_the_dials_that_exist():
    """An empty sweep would pass for any reason. The suite has plenty of direct
    dials, so this one has to see them, marked ones included."""
    seen = _all_dials()

    assert len(seen) >= 20, f"expected the live tests to be seen, found {len(seen)}: {seen}"
    assert any(marked for _, marked in seen), "no marked dial seen, so marking is unread"


_DIAL = """
import nats
from conftest import broker_url

async def test_dials():
    await nats.connect(broker_url())
"""


def _marked_flags(source: str) -> list[bool]:
    return [marked for _, marked in live_broker_dials(source)]


@pytest.mark.parametrize(
    "address",
    [
        "broker_url()",
        "nats_url",
        "config.nats_url",
        "url",
        "",
        "servers=broker_url()",
    ],
    ids=["broker_url", "fixture-name", "attribute", "bound-name", "no-address", "servers-keyword"],
)
def test_CONTROL_an_unmarked_dial_at_a_non_literal_address_is_reported(address):
    source = _DIAL.replace("broker_url())", f"{address})")
    assert _marked_flags(source) == [False]


@pytest.mark.parametrize(
    "marked",
    [
        _DIAL.replace("async def", "@pytest.mark.nats_required\nasync def"),
        "pytestmark = pytest.mark.nats_required\n" + _DIAL,
        "pytestmark = [pytest.mark.unit, pytest.mark.nats_required]\n" + _DIAL,
        _DIAL.replace(
            "async def test_dials():\n    await nats.connect(broker_url())",
            "@pytest.mark.nats_required\nclass TestLive:\n"
            "    async def test_dials(self):\n        await nats.connect(broker_url())",
        ),
    ],
    ids=["decorator", "module", "module-list", "class"],
)
def test_CONTROL_the_marker_is_read_from_every_place_pytest_takes_it(marked):
    assert _marked_flags(marked) == [True]


@pytest.mark.parametrize(
    "source",
    [
        'import nats\n\nasync def test_dead():\n    await nats.connect("somewhere")\n',
        'import nats\nPLACE = "somewhere"\n\nasync def test_dead():\n'
        "    await nats.connect(PLACE, max_reconnect_attempts=1)\n",
        'import nats\n\nasync def test_dead():\n    place = "somewhere"\n'
        "    await nats.connect(place)\n",
    ],
    ids=["inline-literal", "module-literal", "local-literal"],
)
def test_CONTROL_a_dial_at_a_literal_address_is_not_a_live_dial(source):
    assert live_broker_dials(source) == []


def test_CONTROL_a_call_that_is_not_nats_connect_is_not_a_dial():
    source = "async def test_other():\n    await client.connect(broker_url())\n"
    assert live_broker_dials(source) == []
