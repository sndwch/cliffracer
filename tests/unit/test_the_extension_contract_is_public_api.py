"""The extension contract is importable from `cliffracer`, and nothing private is exported.

Extensions are the framework's primary extension point, so the names an
extension author needs belong to the package's public surface rather than to a
path whose `core.` segment reads as internal. Each is re-exported as the same
object, not a copy or a subclass: `isinstance` checks and `except` clauses have
to agree whichever path an author imported from.
"""

import pytest

import cliffracer
import cliffracer.core.extension as extension
from cliffracer.core.extension import SharedDependency

pytestmark = pytest.mark.unit

CONTRACT = (
    "Extension",
    "ExtensionIsolationError",
    "ExtensionSetupContext",
    "RejectMessage",
    "SharedDependency",
    "WorkerContext",
)


@pytest.mark.parametrize("name", CONTRACT)
def test_the_contract_name_is_exported_from_cliffracer_as_the_same_object(name):
    assert name in cliffracer.__all__, name
    assert getattr(cliffracer, name) is getattr(extension, name), name


def test_the_extension_module_exports_no_private_name():
    private = [name for name in extension.__all__ if name.startswith("_")]

    assert private == [], private


def test_CONTROL_the_contract_list_is_the_extension_modules_public_surface():
    """The parametrised list above is not a hand-picked subset that drifts from the module."""
    assert set(CONTRACT) == set(extension.__all__)


def test_shared_dependency_holds_its_value_under_one_name():
    held = object()
    shared = SharedDependency(held)

    assert shared.value is held
    assert shared.unwrap() is held
    assert not hasattr(shared, "obj")
