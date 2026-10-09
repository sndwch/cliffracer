"""The inspector's source reaches nothing that writes to a broker, and says what the check cannot see.

The source is read by syntax tree. A module is refused if it:

- reaches (calls, or merely names) an attribute that publishes, subscribes, creates, changes or
  removes something, including `request` and nats-py's private `_api_request`, so an alias such as
  `p = jsm.publish` is caught where it is made;
- names such an attribute as a string, so `getattr(jsm, "purge_stream")` is caught;
- calls `getattr` with a name that is not a literal;
- calls a private attribute of any object but `self` and `cls`;
- uses `eval`, `exec`, `compile`, `__import__`, `globals`, `vars` or `locals`.

The one allowance is a single request for `$JS.API.STREAM.NAMES`, which lists streams by subject
and changes nothing: `request` may be reached only inside `reader.stream_names`, as the call
`nc.request(STREAM_NAMES_API, ...)`, and only while `STREAM_NAMES_API` is that exact string.

OUTSIDE THE CHECK: a name assembled at run time by means this list does not name (building an
attribute name from pieces and passing it to something other than `getattr`), and what code the
package imports does. The live check, which counts a stream's consumers and messages before and
after every command, is what covers those.
"""

import ast
from pathlib import Path

import cliffracer_dlq
import pytest

pytestmark = pytest.mark.unit

SOURCE = Path(cliffracer_dlq.__file__).parent

# Every attribute that publishes, subscribes, creates, changes or removes something on a broker.
WRITERS = frozenset(
    {
        "publish",
        "publish_msg",
        "request",
        "_api_request",
        "subscribe",
        "subscribe_bind",
        "pull_subscribe",
        "pull_subscribe_bind",
        "add_consumer",
        "delete_consumer",
        "add_stream",
        "update_stream",
        "delete_stream",
        "purge_stream",
        "delete_msg",
        "secure_delete_msg",
        "create_key_value",
        "create_object_store",
        "delete_key_value",
        "delete_object_store",
        "drain",
        "flush",
        "ack",
        "nak",
        "term",
        "in_progress",
    }
)
DYNAMIC = frozenset({"eval", "exec", "compile", "__import__", "globals", "vars", "locals"})
STREAM_NAMES = "$JS.API.STREAM.NAMES"


def _parents(tree: ast.AST) -> dict[ast.AST, ast.AST]:
    return {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}


def _enclosing_function(node: ast.AST, parents: dict[ast.AST, ast.AST]) -> str | None:
    while node in parents:
        node = parents[node]
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            return node.name
    return None


def _module_constant(tree: ast.Module, name: str) -> object:
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(
            isinstance(t, ast.Name) and t.id == name for t in node.targets
        ):
            return node.value.value if isinstance(node.value, ast.Constant) else None
    return None


def _is_the_stream_names_request(
    node: ast.Attribute, path: Path, tree: ast.Module, parents: dict[ast.AST, ast.AST]
) -> bool:
    call = parents.get(node)
    if not (isinstance(call, ast.Call) and call.func is node and call.args):
        return False
    first = call.args[0]
    exact = (
        isinstance(first, ast.Name)
        and first.id == "STREAM_NAMES_API"
        and _module_constant(tree, "STREAM_NAMES_API") == STREAM_NAMES
    ) or (isinstance(first, ast.Constant) and first.value == STREAM_NAMES)
    return (
        path.name == "reader.py" and _enclosing_function(node, parents) == "stream_names" and exact
    )


def violations(root: Path) -> list[str]:
    """What in the modules under `root` could write to a broker, as `file:line what`."""
    found: list[str] = []
    for path in sorted(root.rglob("*.py")):
        tree = ast.parse(path.read_text(), str(path))
        parents = _parents(tree)
        for node in ast.walk(tree):
            where = f"{path.name}:{getattr(node, 'lineno', 0)}"
            if isinstance(node, ast.Attribute):
                if node.attr in WRITERS and not (
                    node.attr == "request"
                    and _is_the_stream_names_request(node, path, tree, parents)
                ):
                    found.append(f"{where} reaches {node.attr}")
                if node.attr in ("__getattribute__", "__dict__"):
                    found.append(f"{where} reaches {node.attr}")
                call = parents.get(node)
                receiver_is_self = isinstance(node.value, ast.Name) and node.value.id in (
                    "self",
                    "cls",
                )
                if (
                    isinstance(call, ast.Call)
                    and call.func is node
                    and node.attr.startswith("_")
                    and not node.attr.startswith("__")
                    and not receiver_is_self
                ):
                    found.append(f"{where} calls the private {node.attr}")
            elif isinstance(node, ast.Constant) and isinstance(node.value, str):
                if node.value in WRITERS:
                    found.append(f"{where} names {node.value!r} as a string")
            elif isinstance(node, ast.Name) and node.id in DYNAMIC:
                found.append(f"{where} uses {node.id}")
            if (
                isinstance(node, ast.Call)
                and isinstance(node.func, ast.Name)
                and node.func.id == "getattr"
                and len(node.args) >= 2
                and not (
                    isinstance(node.args[1], ast.Constant) and isinstance(node.args[1].value, str)
                )
            ):
                found.append(f"{where} calls getattr with a name that is not a literal")
    return found


def test_the_package_reaches_nothing_that_writes():
    assert list(SOURCE.rglob("*.py")), "no source was read"
    assert violations(SOURCE) == []


def test_the_one_request_it_makes_is_the_stream_names_request_in_the_reader():
    requests = []
    for path in sorted(SOURCE.rglob("*.py")):
        tree = ast.parse(path.read_text())
        requests += [
            f"{path.name}:{_enclosing_function(n, _parents(tree))}"
            for n in ast.walk(tree)
            if isinstance(n, ast.Attribute) and n.attr == "request"
        ]

    assert requests == ["reader.py:stream_names"]


def test_the_inspector_reaches_the_broker_only_to_connect_ask_for_names_read_a_stream_and_close():
    names = set()
    for path in SOURCE.rglob("*.py"):
        names |= {
            n.func.attr
            for n in ast.walk(ast.parse(path.read_text()))
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
        }

    reaches = names & {"get_msg", "stream_info", "request", "jsm", "connect", "close"}
    assert reaches == {"get_msg", "stream_info", "request", "jsm", "connect", "close"}


def _planted(tmp_path: Path, source: str, name: str = "bad.py") -> list[str]:
    (tmp_path / name).write_text(source)
    return violations(tmp_path)


@pytest.mark.parametrize(
    ("source", "expected"),
    [
        ("async def go(jsm):\n    await jsm.purge_stream('DLQ')\n", "reaches purge_stream"),
        ("async def go(js):\n    await js.publish('x', b'')\n", "reaches publish"),
        ("async def go(nc):\n    await nc.subscribe('x')\n", "reaches subscribe"),
        ("def go(jsm):\n    p = jsm.publish\n    return p\n", "reaches publish"),
        (
            "async def go(jsm):\n    await jsm._api_request('$JS.API.STREAM.PURGE.DLQ', b'')\n",
            "reaches _api_request",
        ),
        ("async def go(jsm):\n    await jsm._anything('x')\n", "calls the private _anything"),
        (
            "async def go(jsm):\n    await getattr(jsm, 'purge_stream')('DLQ')\n",
            "names 'purge_stream' as a string",
        ),
        (
            "async def go(jsm, name):\n    await getattr(jsm, name)('DLQ')\n",
            "getattr with a name that is not a literal",
        ),
        ("def go():\n    eval('1')\n", "uses eval"),
        ("def go():\n    return __import__('os')\n", "uses __import__"),
        ("def go(jsm):\n    return jsm.__dict__\n", "reaches __dict__"),
    ],
)
def test_CONTROL_each_way_a_writer_could_be_reached_is_seen(tmp_path, source, expected):
    assert any(expected in line for line in _planted(tmp_path, source)), source


def test_CONTROL_a_request_outside_the_one_allowed_place_is_seen(tmp_path):
    seen = _planted(tmp_path, "async def go(nc):\n    await nc.request('x', b'')\n", "reader.py")

    assert any("reaches request" in line for line in seen)


def test_CONTROL_a_request_for_another_subject_in_the_allowed_function_is_seen(tmp_path):
    source = (
        "STREAM_NAMES_API = '$JS.API.STREAM.NAMES'\n\n"
        "async def stream_names(nc):\n    await nc.request('$JS.API.STREAM.PURGE.DLQ', b'')\n"
    )

    assert any("reaches request" in line for line in _planted(tmp_path, source, "reader.py"))


def test_CONTROL_the_allowed_constant_changed_to_another_subject_is_seen(tmp_path):
    source = (
        "STREAM_NAMES_API = '$JS.API.STREAM.PURGE.DLQ'\n\n"
        "async def stream_names(nc):\n    await nc.request(STREAM_NAMES_API, b'')\n"
    )

    assert any("reaches request" in line for line in _planted(tmp_path, source, "reader.py"))


def test_CONTROL_the_allowed_request_in_the_allowed_place_is_not_a_violation(tmp_path):
    source = (
        "STREAM_NAMES_API = '$JS.API.STREAM.NAMES'\n\n"
        "async def stream_names(nc):\n    await nc.request(STREAM_NAMES_API, b'{}')\n"
    )

    assert _planted(tmp_path, source, "reader.py") == []


def test_CONTROL_the_same_request_in_another_module_is_seen(tmp_path):
    source = (
        "STREAM_NAMES_API = '$JS.API.STREAM.NAMES'\n\n"
        "async def stream_names(nc):\n    await nc.request(STREAM_NAMES_API, b'{}')\n"
    )

    assert any("reaches request" in line for line in _planted(tmp_path, source, "other.py"))


def test_CONTROL_a_literal_getattr_of_a_harmless_name_is_not_a_violation(tmp_path):
    assert _planted(tmp_path, "def go(m):\n    return getattr(m, 'headers', None)\n") == []


def test_CONTROL_the_exact_request_in_another_function_of_the_reader_is_seen(tmp_path):
    source = (
        "STREAM_NAMES_API = '$JS.API.STREAM.NAMES'\n\n"
        "async def somewhere_else(nc):\n    await nc.request(STREAM_NAMES_API, b'{}')\n"
    )

    assert any("reaches request" in line for line in _planted(tmp_path, source, "reader.py"))


def test_CONTROL_a_private_call_on_self_is_not_a_violation(tmp_path):
    source = "class C:\n    def go(self):\n        return self._helper()\n\n    def _helper(self):\n        return 1\n"

    assert _planted(tmp_path, source) == []
