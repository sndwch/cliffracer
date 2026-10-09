"""Transport tests receive their transport from the fixture rather than build one.

`tests/transport/` runs the library over a transport that is meant to be
swappable. A test that constructs its own `InMemoryBroker` in its body pins the
in-memory implementation there, so parametrising the fixture over a second
backend leaves that test on the in-memory broker and reports green from both
legs. The fixture is only load-bearing while every test actually asks for it.

The sweep reads the syntax tree of each module in that directory, so a
construction reintroduced anywhere in it fails here rather than quietly
narrowing what a parametrised fixture reaches.
"""

import ast
from pathlib import Path

import pytest

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
TRANSPORT_DIR = REPO / "tests" / "transport"

TRANSPORT_CLASS = "InMemoryBroker"
FIXTURE = "mock_transport"

# test_introspection.py exercises introspection primitives that read a service's
# own registries and reach no transport. Each is named here rather than inferred
# from whether it happens to take the fixture: a module that silently stopped
# taking it would otherwise leave the sweep smaller without saying so.
MODULES_WITHOUT_A_TRANSPORT = frozenset(
    {
        "test_introspection.py",
        # Tests the JetStream message double the direct-dispatch tests build; the broker does
        # not model JetStream, so there is no transport for it to take.
        "test_the_message_double_refuses_what_the_real_message_refuses.py",
    }
)

# The count is a literal so that a module added, renamed or deleted is something
# a person reads, rather than a number that follows the tree wherever it goes.
MODULES_TAKING_THE_FIXTURE = 4


def transport_test_modules() -> list[Path]:
    """Every test module in the transport directory."""
    return sorted(TRANSPORT_DIR.glob("test_*.py"))


def _names_the_transport(node: ast.expr | None) -> bool:
    """True when an expression refers to the transport class by name.

    Covers the bare name and an attribute reference (`testing.InMemoryBroker`),
    because both reach the same class and only one of them is a bare `Name`.
    """
    if isinstance(node, ast.Name):
        return node.id == TRANSPORT_CLASS
    if isinstance(node, ast.Attribute):
        return node.attr == TRANSPORT_CLASS
    return False


def constructions_in(source: str) -> list[int]:
    """Line numbers at which the source pins itself to the transport class.

    Three shapes reach it, and a sweep that reads only the first fails open:

    - constructing it, `InMemoryBroker()` or `testing.InMemoryBroker()`;
    - subclassing it, `class Dropping(InMemoryBroker)`, whose instances are
      the in-memory transport under another name;
    - binding it to another name, `Stand = InMemoryBroker`, which moves the
      construction out of reach of a search for the class's own name.
    """
    found: list[int] = []
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Call) and _names_the_transport(node.func):
            found.append(node.lineno)
        elif isinstance(node, ast.ClassDef) and any(_names_the_transport(b) for b in node.bases):
            found.append(node.lineno)
        elif isinstance(node, ast.Assign) and _names_the_transport(node.value):
            found.append(node.lineno)
    return sorted(found)


def takes_the_fixture(source: str) -> bool:
    """True when some function in the source accepts the fixture as a parameter."""
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef):
            if any(arg.arg == FIXTURE for arg in node.args.args):
                return True
    return False


def test_no_transport_test_builds_its_own_transport():
    """A test in the transport directory takes the transport, it does not make one."""
    offenders = {
        path.name: lines
        for path in transport_test_modules()
        if (lines := constructions_in(path.read_text()))
    }
    assert offenders == {}, (
        "these modules construct the transport directly, which pins them to the "
        f"in-memory one and hides them from a parametrised fixture: {offenders}"
    )


def test_every_module_that_needs_a_transport_takes_the_fixture():
    """The modules that use a transport ask for it, and the count is the one recorded."""
    modules = transport_test_modules()
    assert modules, f"the sweep found no test modules in {TRANSPORT_DIR}, so it reads nothing"

    taking = {p.name for p in modules if takes_the_fixture(p.read_text())}
    expected_without = {p.name for p in modules if p.name in MODULES_WITHOUT_A_TRANSPORT}

    assert taking == {p.name for p in modules} - expected_without, (
        "the set of modules taking the fixture is not the set that needs one; "
        f"taking={sorted(taking)} excluded={sorted(expected_without)}"
    )
    assert len(taking) == MODULES_TAKING_THE_FIXTURE, (
        f"{len(taking)} modules take the fixture, the recorded count is "
        f"{MODULES_TAKING_THE_FIXTURE}: {sorted(taking)}"
    )


def test_the_excluded_module_is_still_there_and_still_needs_no_transport():
    """An exemption whose subject is gone, or now uses a transport, is a stale entry."""
    for name in MODULES_WITHOUT_A_TRANSPORT:
        path = TRANSPORT_DIR / name
        assert path.exists(), f"{name} is exempted but does not exist"
        source = path.read_text()
        assert not constructions_in(source), f"{name} is exempted but builds a transport"
        assert not takes_the_fixture(source), (
            f"{name} is exempted as needing no transport but now takes the fixture, "
            "so the exemption is wrong and the recorded count is short by one"
        )


def test_the_sweep_reads_the_real_tree():
    """The sweep looks at the checked-in modules, not at a fixture of its own."""
    names = {p.name for p in transport_test_modules()}
    assert "test_wire_semantics.py" in names, (
        f"the sweep is not reading {TRANSPORT_DIR}; it found {sorted(names)}"
    )
    assert (TRANSPORT_DIR / "conftest.py").exists(), (
        "the transport conftest is gone, so the fixture these modules take is too"
    )


def test_CONTROL_a_constructed_transport_is_caught():
    """The reader reports a direct construction."""
    assert constructions_in(f"def t():\n    x = {TRANSPORT_CLASS}()\n") == [2]


def test_CONTROL_a_subclassed_transport_is_caught():
    """A subclass is the in-memory transport under another name, and is reported."""
    assert constructions_in(f"class Dropping({TRANSPORT_CLASS}):\n    pass\n") == [1]


def test_CONTROL_an_aliased_transport_is_caught():
    """Binding the class to another name is reported, since the alias constructs it."""
    assert constructions_in(f"Stand = {TRANSPORT_CLASS}\n") == [1]


def test_CONTROL_an_attribute_reference_to_the_transport_is_caught():
    """A reference through a module is the same class, and is reported."""
    assert constructions_in(f"x = conftest.{TRANSPORT_CLASS}()\n") == [1]


def test_CONTROL_an_unrelated_class_and_alias_are_not_caught():
    """The reader does not report a class or alias that is not the transport."""
    assert constructions_in("class Other(Base):\n    pass\n") == []
    assert constructions_in("Stand = SomethingElse\n") == []
    assert constructions_in("x = other.Thing()\n") == []


def test_CONTROL_taking_the_fixture_is_not_a_construction():
    """The reader does not report a test that takes the fixture instead."""
    source = f"def t(other, {FIXTURE}):\n    x = {FIXTURE}\n"
    assert constructions_in(source) == []
    assert takes_the_fixture(source)


def test_CONTROL_a_module_without_the_fixture_is_not_mistaken_for_one_with_it():
    """The fixture reader answers no when nothing takes the fixture."""
    assert not takes_the_fixture("def t(transport_service_factory):\n    pass\n")
