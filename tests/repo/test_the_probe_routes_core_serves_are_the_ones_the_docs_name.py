"""The routes core serves are the routes ADR-0002 allows and the docs name.

ADR-0002 draws the scope line for core's HTTP surface, and its whole content is which routes
are inside it. It named two routes while the listener served four, and nothing compared them, so
the line stopped discriminating: the next route added was no more of a violation than the last
two. This reads the listener's dispatch (the whole `HealthListener` class, so a route handled in a
helper counts) and the ADR, and requires them equal; it also requires the two documents a reader
meets first to name every served route. It is the only guard on this; there is no second one.

What the walk reads, with the literal on either side of a comparison: `path == "/x"`,
`"/x" == path`, `path in ("/x", "/y")` (a tuple, set or list literal), `path in {"/x": ...}` (a
dict literal, by its keys) and `path.startswith("/x")`. What it does not: a regular expression, a
table looked up by path, or a route built from variables. A listener that dispatches that way
stops being read; the control that the walk finds the four routes fails when nothing is found, and
the controls below pin both lists.
"""

import ast
import inspect
import re
import textwrap
from pathlib import Path

import pytest

from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]


def _strings(node: ast.AST) -> set[str]:
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Dict):
        return {
            k.value for k in node.keys if isinstance(k, ast.Constant) and isinstance(k.value, str)
        }
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return {
            e.value for e in node.elts if isinstance(e, ast.Constant) and isinstance(e.value, str)
        }
    return set()


def routes_in_source(source: str) -> set[str]:
    """Every string a comparison against `path`, or a `path.startswith(...)`, can match."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if isinstance(node, ast.Compare):
            operands = [node.left, *node.comparators]
            if any(isinstance(o, ast.Name) and o.id == "path" for o in operands):
                for operand in operands:
                    found |= _strings(operand)
        elif (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "startswith"
            and isinstance(node.func.value, ast.Name)
            and node.func.value.id == "path"
        ):
            for argument in node.args:
                found |= _strings(argument)
    return found


def routes_the_adr_allows(text: str) -> set[str]:
    """The `GET /x` routes named in the body of ADR-0002."""
    match = re.search(r"^## ADR-0002\b.*?(?=^## ADR-|\Z)", text, flags=re.MULTILINE | re.DOTALL)
    assert match, "ADR-0002 is not in decisions.md"
    return set(re.findall(r"`GET (/[a-z]+)`", match.group(0)))


def _served() -> set[str]:
    return routes_in_source(inspect.getsource(HealthListener))


def _adr() -> set[str]:
    return routes_the_adr_allows((REPO / "docs" / "decisions.md").read_text())


def test_the_routes_the_listener_dispatches_are_the_routes_the_adr_names():
    assert _served() == _adr(), _served() ^ _adr()


@pytest.mark.parametrize("document", ["README.md", "docs/ARCHITECTURE.md"])
def test_the_first_documents_a_reader_meets_name_every_served_route(document):
    text = (REPO / document).read_text()

    missing = sorted(route for route in _served() if f"`GET {route}`" not in text)

    assert missing == [], f"{document} does not name {missing}"


def test_CONTROL_the_walk_finds_all_four_routes_today_and_the_adr_names_four():
    assert _served() == {"/live", "/ready", "/health", "/info"}
    assert len(_adr()) == 4


def test_CONTROL_a_route_added_to_the_dispatch_is_a_mismatch_with_the_adr():
    source = inspect.getsource(HealthListener._handle)
    anchor = '            else:\n                await self._respond(writer, 404, {"error": "not found"})'
    assert source.count(anchor) == 1
    added = source.replace(
        anchor,
        '            elif path == "/version":\n                await self._respond(writer, 200, {})\n'
        + anchor,
    )

    assert routes_in_source(added) - _adr() == {"/version"}


def test_CONTROL_a_route_the_adr_stops_naming_is_a_mismatch_with_the_dispatch():
    text = (REPO / "docs" / "decisions.md").read_text()
    narrowed = text.replace("`GET /ready`, ", "", 1)

    assert narrowed != text
    assert _served() - routes_the_adr_allows(narrowed) == {"/ready"}


def test_CONTROL_the_walk_sees_the_literal_on_either_side():
    found = routes_in_source(
        'if path == "/a": pass\nelif "/b" == path: pass\nelif path in ("/c", "/d"): pass\n'
    )

    assert found == {"/a", "/b", "/c", "/d"}


@pytest.mark.parametrize(
    ("branch", "expected"),
    [
        ('path == "/a"', {"/a"}),
        ('"/a" == path', {"/a"}),
        ('path in ("/a", "/b")', {"/a", "/b"}),
        ('path in {"/a", "/b"}', {"/a", "/b"}),
        ('path in ["/a", "/b"]', {"/a", "/b"}),
        ('path in {"/a": 1, "/b": 2}', {"/a", "/b"}),
        ('path.startswith("/a")', {"/a"}),
        ('path.startswith(("/a", "/b"))', {"/a", "/b"}),
    ],
)
def test_CONTROL_the_walk_reads_every_shape_it_says_it_does(branch, expected):
    """A route written in any of these shapes is read, so adding one cannot go unnoticed."""
    source = f"def handle(path):\n    if {branch}:\n        return 1\n"

    assert routes_in_source(source) == expected


@pytest.mark.parametrize(
    "branch", ["re.match('/a', path)", "ROUTES.get(path)", "path == prefix + '/a'"]
)
def test_CONTROL_the_walk_does_not_read_what_the_docstring_says_it_does_not(branch):
    """The limits are real: if one of these were read, the docstring would be out of date."""
    source = f"def handle(path):\n    if {branch}:\n        return 1\n"

    assert routes_in_source(source) == set()


def test_CONTROL_a_route_handled_outside_the_handler_is_read():
    """The walk covers the class, not one method: a dispatch helper's routes count."""
    source = (
        "class Listener:\n"
        "    async def _handle(self, path):\n        return await self._route(path)\n"
        "    async def _route(self, path):\n        if path == '/helper':\n            return 1\n"
    )

    assert routes_in_source(source) == {"/helper"}


def test_CONTROL_the_adr_section_is_read_when_it_is_the_last_one():
    text = "## ADR-0001: first\n`GET /a`\n\n## ADR-0002: last\n`GET /live`, `GET /info`\n"

    assert routes_the_adr_allows(text) == {"/live", "/info"}
