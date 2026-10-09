"""A timer tells an async handler from a sync one without `asyncio.iscoroutinefunction`.

`asyncio.iscoroutinefunction` is deprecated from Python 3.14 and removed in 3.16; the timer asked it
on every firing, so a long-running service logged the warning each time and would fail once it is
gone. `inspect.iscoroutinefunction` answers the same for every handler shape the framework
dispatches. Here the asyncio one raises when asked, as it will once removed, and both kinds of
handler still fire and run once.
"""

import asyncio
import functools

import pytest

from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit


def _removed(*args, **kwargs):
    raise AttributeError("module 'asyncio' has no attribute 'iscoroutinefunction'")


class Service:
    def __init__(self) -> None:
        self.ran: list[str] = []

    async def async_tick(self) -> None:
        self.ran.append("async")

    def sync_tick(self) -> None:
        self.ran.append("sync")

    @staticmethod
    def _wrapped(fn):
        @functools.wraps(fn)
        async def wrapper(self):
            return await fn(self)

        return wrapper

    @_wrapped
    async def decorated_tick(self) -> None:
        self.ran.append("decorated")


@pytest.mark.parametrize(
    ("method", "ran"),
    [("async_tick", "async"), ("sync_tick", "sync"), ("decorated_tick", "decorated")],
)
async def test_a_timer_fires_each_kind_of_handler_without_asking_asyncio(monkeypatch, method, ran):
    monkeypatch.setattr(asyncio, "iscoroutinefunction", _removed, raising=False)
    service = Service()
    timer = Timer(interval=0.1)
    timer.method_name = method
    timer.service_instance = service

    await timer._execute_method()

    assert (service.ran, timer.error_count, timer.last_error) == ([ran], 0, None)
