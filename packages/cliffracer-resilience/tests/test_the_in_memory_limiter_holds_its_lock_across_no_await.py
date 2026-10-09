"""The in-memory limiter's lock brackets code that never awaits, and its docs say so.

`InMemoryRateLimiter` takes an `asyncio.Lock` in every operation. None of those operations awaits
while it holds it, so on one event loop two calls cannot interleave with or without the lock, and
describing it as a feature (the README said "concurrency locks", the docstring "with locks")
promised protection the lock does not give: it is not a thread lock and does not make the limiter
safe across event loops. The text now says what the limiter is (one process, one event loop), and
this keeps the sentence "none of them awaits" true: an `await` added inside a locked block turns the
lock into something that matters, and the docs and this test are the places to revisit then.
"""

import ast
import inspect
from pathlib import Path

import pytest
from cliffracer_resilience import rate_limiter
from cliffracer_resilience.rate_limiter import InMemoryRateLimiter

pytestmark = pytest.mark.unit

README = Path(__file__).resolve().parents[1] / "README.md"


def awaits_inside_a_locked_block(source: str, class_name: str) -> list[str]:
    """Methods of `class_name` that contain an `await` inside an `async with self._lock` block."""
    found = []
    tree = ast.parse(source)
    for cls in (n for n in ast.walk(tree) if isinstance(n, ast.ClassDef) and n.name == class_name):
        for method in (n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)):
            for block in (n for n in ast.walk(method) if isinstance(n, ast.AsyncWith)):
                holds_the_lock = any(
                    isinstance(item.context_expr, ast.Attribute)
                    and item.context_expr.attr == "_lock"
                    for item in block.items
                )
                if holds_the_lock and any(
                    isinstance(inner, ast.Await)
                    for statement in block.body
                    for inner in ast.walk(statement)
                ):
                    found.append(method.name)
    return sorted(set(found))


def locked_methods(source: str, class_name: str) -> list[str]:
    """Methods of `class_name` that take `self._lock` at all, so a vacuous pass is visible."""
    found = []
    for cls in (n for n in ast.walk(ast.parse(source)) if isinstance(n, ast.ClassDef)):
        if cls.name != class_name:
            continue
        for method in (n for n in cls.body if isinstance(n, ast.AsyncFunctionDef)):
            if any(
                isinstance(n, ast.AsyncWith)
                and any(
                    isinstance(i.context_expr, ast.Attribute) and i.context_expr.attr == "_lock"
                    for i in n.items
                )
                for n in ast.walk(method)
            ):
                found.append(method.name)
    return sorted(found)


SOURCE = inspect.getsource(rate_limiter)


def test_no_operation_of_the_in_memory_limiter_awaits_while_it_holds_the_lock():
    assert awaits_inside_a_locked_block(SOURCE, "InMemoryRateLimiter") == []


def test_the_scan_sees_the_operations_that_take_the_lock():
    """So the test above cannot pass by finding no locked block at all."""
    assert locked_methods(SOURCE, "InMemoryRateLimiter") == [
        "acquire",
        "get_retry_after",
        "get_retry_after_for",
        "prune_expired",
        "reset",
    ]


def test_CONTROL_an_await_inside_the_lock_is_found():
    source = (
        "class InMemoryRateLimiter:\n"
        "    async def acquire(self):\n"
        "        async with self._lock:\n"
        "            await self.somewhere()\n"
        "    async def reset(self):\n"
        "        async with self._lock:\n"
        "            self.windows.clear()\n"
        "        await self.after_the_lock()\n"
    )

    assert awaits_inside_a_locked_block(source, "InMemoryRateLimiter") == ["acquire"]


def test_the_docs_do_not_advertise_the_lock_and_say_what_the_limiter_is_for():
    docstring = inspect.getdoc(InMemoryRateLimiter) or ""
    readme = README.read_text()

    assert "with locks" not in docstring
    assert "concurrency lock" not in readme
    assert "none of them awaits" in " ".join(docstring.split())
    assert "one process and one event loop" in readme
