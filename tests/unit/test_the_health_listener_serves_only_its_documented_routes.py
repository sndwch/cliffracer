"""The set of routes core serves is guarded, not just twelve literals that happen to 404.

ADR-0002 gives core's listener exclusively the status probes and `/info`; every other route
belongs in a package. The only guard was a hand-picked list of paths that must answer 404, so a
convenience route added to the dispatch (`/version`, say) passed every test: the list reads the
responses to a few paths, and what decides is the dispatch in `HealthListener._handle`.

This reads the dispatch itself. It collects every string a `path` comparison in `_handle` can
match and requires exactly the documented four. It sees a comparison with `path` on either side
(`path == "/x"`, `"/x" == path`, `path != "/x"`) and `path in ("/x", "/y")`; a route added by
another mechanism (a dict lookup, a startswith) is not seen, which is why the controls below show
what the walk does find. Rewriting an existing route's comparison (`"/info" == path`) changes
nothing it reports.
"""

import ast
import inspect
import textwrap

import pytest

from cliffracer.core.health_listener import HealthListener

pytestmark = pytest.mark.unit

DOCUMENTED = {"/live", "/ready", "/health", "/info"}


def _strings_in(node: ast.AST) -> set[str]:
    """The string literals an operand is, or contains when it is a tuple, list or set."""
    if isinstance(node, ast.Constant) and isinstance(node.value, str):
        return {node.value}
    if isinstance(node, ast.Tuple | ast.List | ast.Set):
        return {
            element.value
            for element in node.elts
            if isinstance(element, ast.Constant) and isinstance(element.value, str)
        }
    return set()


def _routes_in_source(source: str) -> set[str]:
    """Every string a comparison against `path` can match, whichever side `path` is on."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(textwrap.dedent(source))):
        if not isinstance(node, ast.Compare):
            continue
        operands = [node.left, *node.comparators]
        if any(isinstance(operand, ast.Name) and operand.id == "path" for operand in operands):
            for operand in operands:
                found |= _strings_in(operand)
    return found


def _routes_in_handle() -> set[str]:
    return _routes_in_source(inspect.getsource(HealthListener._handle))


def test_the_listener_dispatches_exactly_the_documented_routes():
    assert _routes_in_handle() == DOCUMENTED, _routes_in_handle() ^ DOCUMENTED


def test_CONTROL_the_walk_finds_all_four_documented_routes_today():
    """If the walk finds nothing, the test above would pass for the wrong reason."""
    assert _routes_in_handle() >= DOCUMENTED


def test_CONTROL_the_walk_sees_a_route_however_the_comparison_is_written():
    """The walker itself, on a snippet: `path == x`, `x == path`, `path in (...)`, `path != x`."""
    found = _routes_in_source(
        """
        async def _handle(self):
            if path == "/live":
                pass
            elif "/metrics" == path:
                pass
            elif path in ("/ready", "/version"):
                pass
            elif path != "/other":
                pass
            elif method == "GET":
                pass
        """
    )

    assert found == {"/live", "/metrics", "/ready", "/version", "/other"}
