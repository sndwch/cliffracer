"""An example must leave its ports assignable, so two copies can run at once.

`test_examples_run.py` spawns every example against the broker, and its
bootstrap sets `ServiceConfig.model_fields["health_port"].default = 0` so a
copy gets an OS-assigned port. That only reaches examples which INHERIT the
default. An example passing `health_port=8010`, or `port=8080` to an extension
that binds one, overrides it and binds a fixed port, and a second copy of the tier loses the
race for it.

WHY THE EXISTING RUNTIME GUARD PASSES WHILE THIS IS BROKEN, which is the part
worth understanding. `test_two_copies_of_one_example_do_not_contend_for_a_health_port`
computes `examples_naming_an_explicit_health_port()` and then EXCLUDES those
from the pool it draws from, before picking one example that inherits the
default. So its sample is, by construction, exactly the examples that cannot
contend. It also inspects only health-listener log lines, so a fixed HTTP port
is invisible to it, and it spawns a single named example rather than the
population. Three separate reasons it is green about a thing that is broken.

This reads the source instead: no spawn, no broker, no race, and it names every
offender rather than sampling one.

The three shapes are not decorative -- each was found only after the previous
one read clean:

  kwarg      Server(port=8080)
  default    def __init__(self, health_port: int = 8010)
  name       PORT = 8080  ...  port=PORT

The `default` shape is why `rpc_proxy_example.py` was missing from a scan that
read only keywords, and it binds four ports.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
EXAMPLES = REPO / "examples"

# A port a service actually binds. `nats_url` and friends are not here: those
# are addresses this process dials, not sockets it listens on.
PORT_PARAMS = frozenset({"health_port", "port", "metrics_port", "http_port"})


def _is_pinned(node: ast.expr | None) -> bool:
    """A literal int that is not 0. Zero is the ask: let the OS choose."""
    return isinstance(node, ast.Constant) and isinstance(node.value, int) and node.value != 0


def pinned_ports(path: Path) -> list[str]:
    """Every fixed port this example binds, by line."""
    found: list[str] = []
    tree = ast.parse(path.read_text(), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.keyword) and node.arg in PORT_PARAMS and _is_pinned(node.value):
            found.append(f"{path.name}:{node.lineno} {node.arg}={node.value.value}")
        elif isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            args = node.args
            named = args.posonlyargs + args.args
            for arg, default in zip(
                named[len(named) - len(args.defaults) :], args.defaults, strict=False
            ):
                if arg.arg in PORT_PARAMS and _is_pinned(default):
                    found.append(
                        f"{path.name}:{default.lineno} {node.name}({arg.arg}={default.value})"
                    )
        elif isinstance(node, ast.Assign) and _is_pinned(node.value):
            for target in node.targets:
                if isinstance(target, ast.Name) and "port" in target.id.lower():
                    found.append(f"{path.name}:{node.lineno} {target.id} = {node.value.value}")
    return found


def runnable_examples() -> list[Path]:
    return sorted(p for p in EXAMPLES.rglob("*.py") if not p.name.startswith("_"))


def test_CONTROL_the_reader_finds_all_three_shapes():
    """Each shape, and a port of 0 that must NOT be reported."""
    source = (
        "def build(health_port: int = 8010):\n"
        "    PORT = 8080\n"
        "    a = Server(port=PORT)\n"
        "    b = Server(port=9090)\n"
        "    c = ServiceConfig(health_port=0)\n"
        "    d = connect(nats_url=BROKER_URL, broker_port=4222)\n"
    )
    tmp = Path(__file__).parent / "_control_ports.py"
    tmp.write_text(source)
    try:
        found = pinned_ports(tmp)
    finally:
        tmp.unlink()
    kinds = " ".join(found)
    assert "build(health_port=8010)" in kinds, found
    assert "PORT = 8080" in kinds, found
    assert "port=9090" in kinds, found
    assert "health_port=0" not in kinds, f"port 0 is the ask, not an offence: {found}"
    # An address this process DIALS is not a socket it binds, and a keyword that
    # merely contains "port" is not one of the port parameters.
    assert "nats_url" not in kinds, f"a dialled address is not a bound port: {found}"
    assert "broker_port" not in kinds, f"broker_port is not a bound port: {found}"


def test_no_example_binds_a_fixed_port():
    offenders: list[str] = []
    for path in runnable_examples():
        offenders.extend(pinned_ports(path))
    assert not offenders, (
        "these examples bind a fixed port, so a second copy cannot start and "
        "test_examples_run.py is not concurrency-safe:\n  " + "\n  ".join(offenders)
    )
