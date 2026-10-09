"""Every module that names `auth_context_var` must hold the same object.

A ContextVar is identified by object, not by name. Reloading the module that
defines this one rebinds the module attribute while every module that imported
it keeps the old object, so a value set through one is invisible through the
other and `get_current_user()` reads None for the rest of the process. Nothing
raises; authorization simply stops seeing the caller.
"""

import sys
from contextvars import ContextVar
from types import ModuleType

import pytest

pytestmark = pytest.mark.unit

VAR_NAME = "auth_context"
PACKAGE = "cliffracer_auth"


def _bindings(package: str, var_name: str) -> dict[str, ContextVar]:
    """Every ContextVar named `var_name` reachable as an attribute of a loaded
    module of `package`, keyed by where it was found."""
    found: dict[str, ContextVar] = {}
    for module_name, module in list(sys.modules.items()):
        if module_name != package and not module_name.startswith(package + "."):
            continue
        if module is None:
            continue
        for attribute, value in vars(module).items():
            if isinstance(value, ContextVar) and value.name == var_name:
                found[f"{module_name}.{attribute}"] = value
    return found


def test_every_module_shares_one_auth_context_var():
    import cliffracer_auth  # noqa: F401
    import cliffracer_auth.extension  # noqa: F401
    import cliffracer_auth.simple_auth  # noqa: F401

    bindings = _bindings(PACKAGE, VAR_NAME)

    assert len(bindings) >= 2, f"expected several modules to name it, found {sorted(bindings)}"
    identities = {id(var) for var in bindings.values()}
    assert len(identities) == 1, (
        "these modules hold different ContextVar objects under one name, so a "
        "value set through one is invisible through the others: "
        + ", ".join(f"{where}@{id(var):#x}" for where, var in sorted(bindings.items()))
    )


def test_CONTROL_the_sweep_reports_a_split_identity():
    """The sweep above passes on a healthy interpreter, so on its own it never
    shows it can fail.

    The split is built out of two throwaway modules rather than by reloading a
    real one. `importlib.reload` would produce the same condition and is what
    this guard exists to catch, but it rebinds module-level objects for every
    later test in the session -- the damage lands somewhere else, which is
    exactly the failure being guarded against. Two synthetic modules exercise
    the same code path and leave the session alone.
    """
    fake_package = "_synthetic_auth_pkg"
    first = ModuleType(fake_package)
    first.some_var = ContextVar("auth_context", default=None)
    second = ModuleType(f"{fake_package}.other")
    second.some_var = ContextVar("auth_context", default=None)
    assert first.some_var is not second.some_var

    sys.modules[fake_package] = first
    sys.modules[f"{fake_package}.other"] = second
    try:
        bindings = _bindings(fake_package, "auth_context")
        assert len(bindings) == 2, f"both modules must be seen, saw {sorted(bindings)}"
        assert len({id(v) for v in bindings.values()}) > 1, (
            "two objects under one name is what the sweep must report"
        )
    finally:
        del sys.modules[fake_package]
        del sys.modules[f"{fake_package}.other"]
