"""The service's docstring does not claim it holds no transport logic while it makes the sends.

`CliffracerService` said it "contains no low-level dispatch callbacks or connection transport
logic", and its outbound methods call `nc.request`, `nc.publish` and `js.publish` themselves; the
container's outbound dispatcher builds the context and runs the send hooks but never touches the
wire. The docstring is a statement about the module, so it is checked against the module: a
transport claim and a send in the same file cannot both stand.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

SERVICE = Path(__file__).resolve().parents[2] / "src" / "cliffracer" / "core" / "service.py"
WIRE_CALLS = {("nc", "request"), ("nc", "publish"), ("js", "publish")}


def sends_on_the_wire(source: str) -> set[str]:
    """`self.nc.request`, `self.nc.publish` and `self.js.publish` calls made in `source`."""
    found = set()
    for node in ast.walk(ast.parse(source)):
        if not (isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute)):
            continue
        owner = node.func.value
        if isinstance(owner, ast.Attribute) and (owner.attr, node.func.attr) in WIRE_CALLS:
            found.add(f"{owner.attr}.{node.func.attr}")
    return found


def class_docstring(source: str, name: str) -> str:
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.ClassDef) and node.name == name:
            return ast.get_docstring(node) or ""
    raise AssertionError(f"{name} is not in the module")


def claims_no_transport(docstring: str) -> bool:
    flat = " ".join(docstring.split()).lower()
    return "contains no" in flat and "transport logic" in flat


def test_the_service_docstring_does_not_claim_no_transport_logic_while_the_module_sends():
    source = SERVICE.read_text()

    sends = sends_on_the_wire(source)

    assert sends, "the module no longer sends on the wire: revisit this guard and the docstring"
    assert not claims_no_transport(class_docstring(source, "CliffracerService")), (
        f"the docstring says the service contains no transport logic, and it calls {sorted(sends)}"
    )


def test_the_docstring_names_the_outbound_sends_the_service_makes():
    docstring = " ".join(class_docstring(SERVICE.read_text(), "CliffracerService").split())

    for method in (
        "call_rpc",
        "call_async",
        "call_rpc_no_wait",
        "publish_event",
        "broadcast_message",
    ):
        assert method in docstring, f"the docstring does not name {method}"


def test_CONTROL_the_scan_sees_a_send_and_the_claim_is_recognised():
    code = "class S:\n    async def f(self):\n        await self.nc.publish('a', b'')\n"

    assert sends_on_the_wire(code) == {"nc.publish"}
    assert claims_no_transport(
        "Contains no low-level dispatch callbacks or connection transport logic."
    )
    assert not claims_no_transport("Makes the outbound sends itself.")


def test_CONTROL_a_module_with_no_send_finds_none():
    assert sends_on_the_wire("def f(nc):\n    return nc.request('a')\n") == set()
