"""An exception this package exports is a promise that something raises it.

`RpcServerError` was defined, aliased, exported from two module roots and
documented as what a caller catches when the remote blew up -- and no line in
`src/` ever raised it. A caller writing `except RpcServerError` caught nothing,
forever, and nothing in the suite noticed: the tests that named the class
asserted it existed and subclassed `RpcError`, which is true of a class nobody
raises.

So this reads the tree rather than the class list. Every name the package
exports that resolves to an `RpcError` subclass must have a `raise` site under
`src/`. Aliases resolve to the same class as their canonical name, so
`RpcTimeout` is satisfied by a `raise RpcTimeoutError`.

Scope is deliberately `src/` and the RPC family: extensions under `packages/`
carry their own exception surfaces, and the service-side `ServiceError` tree
has members raised by application code rather than by the framework.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

import cliffracer
from cliffracer.core import exceptions as exc

pytestmark = pytest.mark.repo

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"


def raised_names(source: str) -> set[str]:
    """Every name used as the class in a `raise` statement.

    Reads `raise X(...)`, `raise X` and `raise mod.X(...)`. A bare `raise`
    re-raises whatever is in flight and names nothing, so it is not a raise
    site for this purpose.
    """
    found: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if not isinstance(node, ast.Raise) or node.exc is None:
            continue
        target = node.exc.func if isinstance(node.exc, ast.Call) else node.exc
        if isinstance(target, ast.Name):
            found.add(target.id)
        elif isinstance(target, ast.Attribute):
            found.add(target.attr)
    return found


def raised_classes_under(root: Path) -> set[type]:
    """The exception CLASSES raised anywhere under `root`, resolved through aliases."""
    classes: set[type] = set()
    for path in sorted(root.rglob("*.py")):
        for name in raised_names(path.read_text()):
            candidate = getattr(exc, name, None)
            if isinstance(candidate, type) and issubclass(candidate, BaseException):
                classes.add(candidate)
    return classes


def exported_rpc_family() -> dict[str, type]:
    """Exported names resolving to an `RpcError` subclass, canonical and alias alike."""
    family = {}
    for name in cliffracer.__all__:
        value = getattr(cliffracer, name, None)
        if isinstance(value, type) and issubclass(value, exc.RpcError):
            family[name] = value
    return family


def unraised_in(family: dict[str, type], raised: set[type]) -> list[str]:
    """The exported names whose class nothing raises, directly or through a subclass.

    A base nothing raises directly is still caught by what is raised below it: `except RpcError`
    matches the typed errors. A class with no raised subclass has to be raised itself.
    """
    return sorted(
        f"{name} -> {cls.__name__}"
        for name, cls in family.items()
        if not any(issubclass(candidate, cls) for candidate in raised)
    )


def test_CONTROL_a_base_is_live_through_a_raised_subclass_and_a_leaf_must_be_raised():
    class Base(Exception):
        pass

    class Raised(Base):
        pass

    class NeverRaised(Base):
        pass

    family = {"Base": Base, "Raised": Raised, "NeverRaised": NeverRaised}

    assert unraised_in(family, {Raised}) == ["NeverRaised -> NeverRaised"]
    assert unraised_in(family, set()) == [
        "Base -> Base",
        "NeverRaised -> NeverRaised",
        "Raised -> Raised",
    ]


def test_every_exported_rpc_exception_has_a_raise_site_in_src():
    """A class nobody raises is a promise to callers that nothing keeps."""
    family = exported_rpc_family()
    raised = raised_classes_under(SRC)

    assert len(family) >= 10, f"the export sweep found only {len(family)} names: {sorted(family)}"
    assert len(raised) >= 5, f"the raise sweep found only {len(raised)} classes; is it reading?"

    unraised = unraised_in(family, raised)
    assert not unraised, (
        "exported and documented, but nothing under src/ raises them, so no caller "
        f"can ever catch one: {unraised}"
    )


def test_CONTROL_the_reader_finds_a_raise_and_ignores_a_mention():
    """The near miss: naming a class is not raising it."""
    assert raised_names("raise RpcServerError('x')") == {"RpcServerError"}
    assert raised_names("raise RpcServerError") == {"RpcServerError"}
    assert raised_names("raise exceptions.RpcServerError('x')") == {"RpcServerError"}

    mention = (
        "from cliffracer import RpcServerError\n"
        "DOCS = 'raise RpcServerError when the remote fails'\n"
        "def f():\n"
        "    try:\n"
        "        pass\n"
        "    except RpcServerError:\n"
        "        raise\n"
    )
    assert raised_names(mention) == set(), raised_names(mention)


def test_CONTROL_a_family_member_with_no_raise_site_is_reported():
    """Drive the comparison with a tree that raises nothing, and it must object."""
    family = exported_rpc_family()
    raised_by_an_empty_tree: set[type] = set()

    unraised = [name for name, cls in family.items() if cls not in raised_by_an_empty_tree]
    assert sorted(unraised) == sorted(family), (
        "with nothing raised anywhere, every exported name must be reported; "
        "a comparison that reports fewer is not reading the family"
    )
