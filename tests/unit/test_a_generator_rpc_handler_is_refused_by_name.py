"""An RPC handler written as a generator is refused when the service starts, and by `describe`.

An RPC handler returns one reply. Calling a generator function only builds the generator, so a
handler that yields never runs its body: with an ordinary return annotation (`-> int`) the service
started, `describe` published `int`, every call was answered `internal` (the generator object failed
the return type), and on the fire-and-forget subject it did nothing at all. It is refused by name
instead, whichever decorator marks it: a sync generator whatever its annotation, and an async
generator unless its return is a stream (`AsyncIterator[X]`), which `@rpc` serves item by item and
`@async_rpc`, which has nobody to stream to, refuses.
"""

from collections.abc import AsyncIterator, Iterator

import pytest

from cliffracer import CliffracerService, ServiceConfig, async_rpc, rpc
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.typed_rpc import UntypedHandler
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit

GENERATOR = "an RPC handler cannot be a generator"


async def an_async_generator_returning_int(self, n: int) -> int:  # type: ignore[misc]
    for i in range(n):
        yield i


async def an_async_generator_returning_an_iterator(self, n: int) -> AsyncIterator[int]:
    for i in range(n):
        yield i


def a_generator_returning_int(self, n: int) -> int:  # type: ignore[misc]
    yield from range(n)


def a_generator_returning_an_iterator(self, n: int) -> Iterator[int]:
    yield from range(n)


async def an_ordinary_handler(self, n: int) -> int:
    return n


HANDLERS = [
    pytest.param(an_async_generator_returning_int, id="async-generator-int"),
    pytest.param(a_generator_returning_int, id="generator-int"),
    pytest.param(a_generator_returning_an_iterator, id="generator-iterator"),
]
DECORATORS = [pytest.param(rpc, id="rpc"), pytest.param(async_rpc, id="async_rpc")]


def service_with(handler, decorator) -> type[CliffracerService]:
    def __init__(self):
        CliffracerService.__init__(
            self, ServiceConfig(name="svc", subject_prefix=None, health_port=0)
        )

    handler.__name__ = "count"
    return type("Svc", (CliffracerService,), {"__init__": __init__, "count": decorator(handler)})


@pytest.mark.parametrize("decorator", DECORATORS)
@pytest.mark.parametrize("handler", HANDLERS)
def test_discovery_refuses_a_generator_handler_by_name(handler, decorator):
    service = service_with(handler, decorator)()

    with pytest.raises(UntypedHandler) as caught:
        HandlerDiscovery.discover(service, service.config)

    assert str(caught.value).startswith(f"Svc.count: {GENERATOR}"), str(caught.value)


@pytest.mark.parametrize("handler", HANDLERS)
def test_describe_refuses_it_too_so_it_never_publishes_a_contract_the_service_cannot_keep(handler):
    with pytest.raises(UntypedHandler, match=f"Svc.count: {GENERATOR}"):
        describe(service_with(handler, rpc))


@pytest.mark.parametrize("decorator", DECORATORS)
def test_CONTROL_an_ordinary_handler_is_discovered_and_described(decorator):
    cls = service_with(an_ordinary_handler, decorator)
    service = cls()

    registry = HandlerDiscovery.discover(service, service.config)

    assert "count" in registry.rpc_handlers
    assert [m.name for m in describe(cls).methods] == ["count"]


def test_an_async_generator_returning_a_stream_is_served_as_one_by_rpc():
    service = service_with(an_async_generator_returning_an_iterator, rpc)()

    registry = HandlerDiscovery.discover(service, service.config)

    assert registry.rpc_specs["count"].streams


def test_an_async_generator_returning_a_stream_is_refused_by_name_by_async_rpc():
    service = service_with(an_async_generator_returning_an_iterator, async_rpc)()

    with pytest.raises(UntypedHandler) as caught:
        HandlerDiscovery.discover(service, service.config)

    assert str(caught.value).startswith(
        "Svc.count: a handler that streams its reply cannot be @async_rpc"
    ), str(caught.value)
