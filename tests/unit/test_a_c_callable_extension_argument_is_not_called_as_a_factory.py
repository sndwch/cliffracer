"""An extension argument that is a C callable is copied, not called as a zero-argument factory.

`_safe_clone_arg` calls a callable argument that takes no arguments and uses its product, and
`_takes_no_arguments` decides which callables those are by reading their signature. On Python 3.12
many C callables have no signature of their own and are reported as exactly `(*args, **kwargs)`,
which binds no arguments: `operator.itemgetter("id")` was called at service build time and the
build failed, where on 3.13 it reports `(obj, /)` and is copied. A `(*args, **kwargs)` signature
on a callable with no Python code behind it now counts as unreadable, so it is not a factory on
either version. Python functions, lambdas, partials and Python callables keep their reading.
"""

import functools
import inspect
import operator
import sqlite3

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension, _has_python_code, _takes_no_arguments

pytestmark = pytest.mark.unit


class KeyExt(Extension):
    def __init__(self, key):
        self.key = key


@pytest.mark.parametrize(
    ("key", "sample"),
    [
        (operator.itemgetter("id"), {"id": 7}),
        (operator.attrgetter("real"), 7),
        (operator.methodcaller("bit_length"), 7),
    ],
    ids=["itemgetter", "attrgetter", "methodcaller"],
)
def test_a_service_whose_extension_holds_a_c_callable_builds_and_keeps_the_callable(key, sample):
    class Svc(CliffracerService):
        pick = KeyExt(key)

    with pytest.warns(FutureWarning, match="copied for each service instance"):
        service = Svc(ServiceConfig(name="svc", health_port=0))

    assert type(service.pick.key) is type(key)
    assert service.pick.key(sample) == key(sample)


@pytest.mark.parametrize(
    "c_callable",
    [
        operator.itemgetter("id"),
        operator.attrgetter("real"),
        operator.methodcaller("bit_length"),
        sqlite3.connect(":memory:"),
    ],
    ids=["itemgetter", "attrgetter", "methodcaller", "sqlite3-connection"],
)
def test_a_c_callable_is_not_a_zero_argument_factory(c_callable):
    assert _takes_no_arguments(c_callable) is False


def _varargs(*args, **kwargs):
    return 1


def _one_argument(x):
    return x


class _PythonVarargsCallable:
    def __call__(self, *args, **kwargs):
        return 1


class _PythonVarargsClass:
    def __init__(self, *args, **kwargs):
        pass


@pytest.mark.parametrize(
    ("thing", "expected"),
    [
        (_varargs, True),
        (_PythonVarargsCallable(), True),
        (functools.partial(_varargs), True),
        (functools.partial(_PythonVarargsClass), True),
        (_one_argument, False),
    ],
    ids=[
        "python-varargs-function",
        "python-varargs-callable",
        "partial-of-python-varargs",
        "partial-of-a-python-varargs-class",
        "function-needing-an-argument",
    ],
)
def test_CONTROL_python_callables_keep_their_reading(thing, expected):
    assert _takes_no_arguments(thing) is expected


def _no_python_call(signature: inspect.Signature):
    """A callable that runs no Python code of its own and reports exactly `signature`.

    Its class's `__call__` is a `functools.partial`, which has no `__code__`, so it stands for a C
    callable, and `__signature__` is what `inspect.signature` reads for it."""

    class NoPythonCall:
        __call__ = functools.partial(lambda *args, **kwargs: None)
        __signature__ = signature

    return NoPythonCall()


_P = inspect.Parameter
BARE = inspect.Signature([_P("args", _P.VAR_POSITIONAL), _P("kwargs", _P.VAR_KEYWORD)])
KEYWORD_ONLY = inspect.Signature([_P("key", _P.KEYWORD_ONLY, default=None)])


@pytest.mark.parametrize(
    ("signature", "expected"),
    [
        # Binds no arguments and is no reading at all: with no Python code, not a factory.
        pytest.param(BARE, False, id="bare-varargs-and-no-python-code"),
        # A readable signature that binds no arguments: a factory, Python code or not.
        pytest.param(KEYWORD_ONLY, True, id="keyword-only-and-no-python-code"),
    ],
)
def test_the_bare_varargs_exclusion_needs_both_a_bare_signature_and_no_python_code(
    signature, expected
):
    thing = _no_python_call(signature)
    assert inspect.signature(thing) == signature, "the stand-in no longer reports its signature"
    assert not _has_python_code(thing), (
        "_has_python_code reports Python code for a callable with no __code__: either it changed, "
        "or the stand-in now reaches Python code (then the rows test the wrong branch)"
    )

    assert _takes_no_arguments(thing) is expected


def test_a_zero_argument_python_factory_argument_is_called_and_its_product_held():
    class Svc(CliffracerService):
        pick = KeyExt(lambda: {"fresh": 1})

    service = Svc(ServiceConfig(name="svc", health_port=0))

    assert service.pick.key == {"fresh": 1}
