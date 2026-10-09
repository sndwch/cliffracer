"""A timer's `token_factory` may be a coroutine function, as `outbound_token_factory` may.

The auth README describes `outbound_token_factory` as returning the token "or an awaitable of it,
the way `token_factory` does for a timer". The timer called the factory and read `.lower()` on
the result, so an `async def` factory raised `AttributeError` on every firing, counted an error,
and never ran the method.
"""

import pytest

from cliffracer.core.timer import Timer

pytestmark = pytest.mark.unit


class Service:
    def __init__(self) -> None:
        self.seen: list[dict[str, str]] = []
        self.ran = 0

    async def _run_worker(self, ctx, fn):
        self.seen.append(dict(ctx.headers))
        return await fn()

    async def tick(self) -> None:
        self.ran += 1


async def _fire(factory) -> tuple[Service, Timer]:
    service = Service()
    t = Timer(interval=0.1, token_factory=factory)
    t.method_name = "tick"
    t.service_instance = service
    await t._execute_method()
    return service, t


@pytest.mark.parametrize(
    ("minted", "authorization"),
    [("abc", "Bearer abc"), ("Bearer abc", "Bearer abc"), ("", None)],
    ids=["bare", "prefixed", "empty"],
)
async def test_an_async_factory_is_awaited_and_its_token_sent(minted, authorization):
    async def factory() -> str:
        return minted

    service, t = await _fire(factory)

    expected = {} if authorization is None else {"authorization": authorization}
    assert service.seen == [expected]
    assert (service.ran, t.error_count, t.last_error) == (1, 0, None)


async def test_CONTROL_a_sync_factory_still_works():
    service, t = await _fire(lambda: "abc")

    assert service.seen == [{"authorization": "Bearer abc"}]
    assert (service.ran, t.error_count) == (1, 0)
