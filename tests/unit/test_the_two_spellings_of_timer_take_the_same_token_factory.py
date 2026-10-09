"""`cliffracer.timer` and `cliffracer.core.timer.timer` are one decorator and type `token_factory` alike.

`cliffracer.timer` forwards to the core decorator, which builds it. Its signature typed the factory
as `Callable[[], str]` and its docstring said "returning a bearer token" after the core decorator
began to take a coroutine function too, so a type checker refused `@timer(token_factory=async_fn)`
on one spelling and not the other.
"""

import inspect
import typing

import pytest

import cliffracer
from cliffracer.core import timer as core_timer

pytestmark = pytest.mark.unit


def _annotation(decorator) -> str:
    hints = typing.get_type_hints(decorator)
    return str(hints["token_factory"])


def test_the_exported_decorator_types_token_factory_like_the_one_that_builds_it():
    assert _annotation(cliffracer.timer) == _annotation(core_timer.timer)


def test_the_exported_decorator_accepts_an_awaitable_returning_factory():
    annotation = _annotation(cliffracer.timer)
    assert "Awaitable" in annotation, annotation


def test_the_exported_decorator_documents_the_awaitable():
    assert "awaitable" in (inspect.getdoc(cliffracer.timer) or "").lower()
