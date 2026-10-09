"""A zero-argument factory is asked about, called once, and never swallowed.

`_safe_clone_arg` treated any non-class callable as a factory and CALLED it,
guarded by `except TypeError: pass` and `except Exception: pass`. Two things
were wrong with that, and the second is the one that produced a broken service
that looked bound.

ASKING BY CALLING. `TypeError` was the test for "this needs arguments, so it is
not a factory". But a zero-argument factory whose body raises `TypeError` -- an
ordinary bug -- raises the same exception, and was reported as "not a factory".
The raw callable was then deep-copied into the extension in place of its
product, so the failure surfaced later as `'function' object has no attribute
...` somewhere else entirely. `inspect.signature(...).bind()` asks the question
instead of guessing from the answer.

SWALLOWING. `except Exception: pass` ran the factory's side effects, discarded
what it raised, and proceeded. ADR-0005 says "passing uncopyable objects or
mutating extension specifications at runtime fails loudly"; a factory that blew
up after writing to a database failed as quietly as it is possible to fail. It
now raises `ExtensionIsolationError` naming the factory, what it raised, and
the escape hatch.

WHAT DELIBERATELY DID NOT CHANGE. A zero-argument callable is still invoked at
bind time and the extension still receives its RESULT -- including a callable
OBJECT, which is invoked through `__call__`. That surprises people, and the
remedy is `SharedDependency(...)`, but it is long-standing behaviour with tests
of its own, and changing it is a break that belongs in a major rather than on a
defect ticket. It is documented now instead of being merely true.
"""

import functools

import pytest

from cliffracer.core.extension import (
    ExtensionIsolationError,
    SharedDependency,
    _safe_clone_arg,
)

pytestmark = pytest.mark.unit


# --- a factory that raises is reported, not swallowed ------------------------


def test_a_factory_that_raises_is_reported_rather_than_swallowed():
    """The defect: side effects ran, the error vanished, binding continued."""
    ran = []

    def factory():
        ran.append("side effect")
        raise RuntimeError("factory blew up")

    with pytest.raises(ExtensionIsolationError) as exc:
        _safe_clone_arg(factory)

    assert ran == ["side effect"], "the factory still runs; what changes is the reporting"
    assert "factory" in str(exc.value)
    assert "RuntimeError" in str(exc.value), str(exc.value)
    assert "blew up" in str(exc.value), str(exc.value)
    assert isinstance(exc.value.__cause__, RuntimeError)


def test_a_factory_raising_TypeError_is_not_mistaken_for_a_non_factory():
    """The worst case of the old probe, because `TypeError` was its signal.

    A zero-argument factory whose body raises `TypeError` is an ordinary bug and
    was indistinguishable from "this callable needs arguments".
    """

    def factory():
        raise TypeError("a real bug inside the factory")

    with pytest.raises(ExtensionIsolationError) as exc:
        _safe_clone_arg(factory)

    assert "TypeError" in str(exc.value), str(exc.value)


def test_the_error_names_the_escape_hatch():
    """A caller who meant to pass the callable itself needs to be told how."""

    def factory():
        raise RuntimeError("boom")

    with pytest.raises(ExtensionIsolationError) as exc:
        _safe_clone_arg(factory)

    assert "SharedDependency" in str(exc.value), str(exc.value)


# --- the signature is asked about, not guessed from a TypeError --------------


def _needs_one(a):
    return a


class NeedsOne:
    def __call__(self, a):
        return a


@pytest.mark.parametrize(
    ("label", "arg"),
    [
        ("lambda", lambda a: a),
        ("function", _needs_one),
        ("callable object", NeedsOne()),
        ("partial still missing an argument", functools.partial(lambda x, y: x, 1)),
    ],
)
def test_CONTROL_a_callable_needing_an_argument_is_copied_rather_than_called(label, arg):
    """The half that must not become "call everything and see".

    Without this, making the raising case loud could be done by calling
    everything and reporting whatever came back -- which would turn every
    one-argument callback into an `ExtensionIsolationError` at bind time.
    """
    out = _safe_clone_arg(arg)

    assert not isinstance(out, str), f"{label} was invoked: {out!r}"
    assert callable(out), f"{label} should still be a callable, got {out!r}"


def test_CONTROL_a_callable_whose_signature_cannot_be_read_is_not_called():
    """Some C builtins have no readable signature; copying is the safe direction.

    Calling something that was not a factory is a side effect at bind time and
    cannot be undone. Copying something that WAS meant as a factory shows up at
    its first use. `max`, `dir`, `iter` and `vars` are real examples.
    """
    import builtins

    for name in ("max", "dir", "iter", "vars"):
        arg = getattr(builtins, name)
        assert _safe_clone_arg(arg) is arg, name


# --- what deliberately did not change ----------------------------------------


def test_CONTROL_a_zero_argument_callable_is_still_invoked():
    """Long-standing behaviour, kept on purpose and documented rather than fixed."""
    assert _safe_clone_arg(lambda: "product") == "product"


def test_CONTROL_a_callable_object_is_still_invoked():
    """The surprising half of the same rule, stated so it is not rediscovered."""

    class Limiter:
        def __call__(self):
            return "called"

    assert _safe_clone_arg(Limiter()) == "called"


def test_CONTROL_shared_dependency_still_passes_the_callable_through():
    """The escape hatch the error message names has to work."""
    cb = lambda: "product"  # noqa: E731 - the callable itself is the subject

    assert _safe_clone_arg(SharedDependency(cb)) is cb


def test_CONTROL_a_class_is_never_invoked():
    """`isinstance(arg, type)` is checked before any of this."""

    class Thing:
        def __init__(self):
            self.x = 1

    assert _safe_clone_arg(Thing) is Thing
