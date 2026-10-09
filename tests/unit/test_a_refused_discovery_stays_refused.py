"""A service whose handlers discovery refused is refused every time, not once.

`Container.discover_handlers` marked discovery done BEFORE running it, so when an invariant
raised (pull without a durable, a duplicate subject, an untyped handler...) the next call took the
"already discovered" early exit and returned normally with the refused handler registered. A
second `start()` on the same instance (a supervisor that retries, or a caller that catches the
`ConfigurationError` and tries again) therefore went on to subscribe what the framework had just
refused. The first refusal is now remembered and raised on every later call.
"""

import pytest

from cliffracer import CliffracerService, ConfigurationError, ServiceConfig, listener, rpc
from cliffracer.core.typed_rpc import UntypedHandler

pytestmark = pytest.mark.unit


def _pull_without_a_durable():
    class S(CliffracerService):
        @listener("events.pull", pull=True)
        async def on_pull(self, subject: str) -> None:
            pass

    return S


def _a_duplicate_subject():
    class S(CliffracerService):
        @listener("events.thing", fanout=True)
        async def first(self, subject: str) -> None:
            pass

        @listener("events.thing", fanout=True)
        async def second(self, subject: str) -> None:
            pass

    return S


def _undeclared_fanout():
    class S(CliffracerService):
        @listener("events.thing")
        async def on_thing(self, subject: str) -> None:
            pass

    return S


def _an_untyped_handler():
    class S(CliffracerService):
        @rpc
        async def echo(self, text: str):  # no return annotation
            return text

    return S


@pytest.mark.parametrize(
    ("build", "error"),
    [
        (_pull_without_a_durable, ConfigurationError),
        (_a_duplicate_subject, ConfigurationError),
        (_undeclared_fanout, ConfigurationError),
        (_an_untyped_handler, UntypedHandler),
    ],
)
def test_the_second_discovery_raises_the_first_refusal_instead_of_succeeding(build, error):
    svc = build()(ServiceConfig(name="refused", health_port=0))

    with pytest.raises(error) as first:
        svc.container.discover_handlers()
    with pytest.raises(error) as second:
        svc.container.discover_handlers()

    assert str(second.value) == str(first.value)


@pytest.mark.parametrize("build", [_pull_without_a_durable, _a_duplicate_subject])
async def test_a_second_start_is_refused_too_and_never_reaches_the_broker(build, monkeypatch):
    svc = build()(ServiceConfig(name="refused", health_port=0))
    connected: list[int] = []
    monkeypatch.setattr(svc.container, "connect", lambda *a, **k: connected.append(1))

    for _ in range(2):
        with pytest.raises(ConfigurationError):
            await svc.start()

    assert connected == [], "a refused service reached the broker on a retry"


def test_CONTROL_a_service_that_discovered_cleanly_is_discovered_once_and_stays_fine():
    class S(CliffracerService):
        @listener("events.thing", fanout=True)
        async def on_thing(self, subject: str) -> None:
            pass

    svc = S(ServiceConfig(name="fine", health_port=0))

    svc.container.discover_handlers()
    handlers = dict(svc.container.registry.event_handlers)
    svc.container.discover_handlers()  # idempotent, and does not raise

    assert dict(svc.container.registry.event_handlers) == handlers
