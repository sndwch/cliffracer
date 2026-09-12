"""Tests verifying correlation IDs are scoped per message across dispatches."""

import asyncio

import pytest

from cliffracer.core.correlation import CorrelationContext, correlation_id_var

pytestmark = pytest.mark.unit


def test_new_id_unless_given_ignores_the_ambient_context():
    """Ensure generating a new ID ignores any existing ambient context."""
    token = correlation_id_var.set("corr_fromapreviousmessage")
    try:
        first = CorrelationContext.new_id_unless_given(None)
        second = CorrelationContext.new_id_unless_given(None)
        assert first != "corr_fromapreviousmessage", first
        assert second != "corr_fromapreviousmessage", second
        assert first != second, (first, second)
        assert first.startswith("corr_") and len(first) == len("corr_") + 16
    finally:
        correlation_id_var.reset(token)


def test_new_id_unless_given_keeps_an_id_it_is_given():
    assert (
        CorrelationContext.new_id_unless_given("corr_fromthepublisher") == "corr_fromthepublisher"
    )


def test_new_id_unless_given_does_not_write_the_context():
    """Ensure new_id_unless_given does not modify the context variable."""
    token = correlation_id_var.set(None)
    try:
        CorrelationContext.new_id_unless_given(None)
        assert correlation_id_var.get() is None
    finally:
        correlation_id_var.reset(token)


def test_get_or_create_id_still_reuses_the_context():
    """Ensure get_or_create_id reuses existing context when present."""
    token = correlation_id_var.set("corr_httprequestscope")
    try:
        assert CorrelationContext.get_or_create_id() == "corr_httprequestscope"
    finally:
        correlation_id_var.reset(token)


# ---- the dispatch paths ----------------------------------------------------


class _Msg:
    """The minimum of a NATS msg the dispatch paths read."""

    def __init__(self, data: bytes, subject: str = "events.thing.happened"):
        self.data = data
        self.subject = subject
        self.headers = None

    async def ack(self):
        pass


def _service():
    """A service with one event handler, wired without touching NATS."""
    from cliffracer import CliffracerService, ServiceConfig, listener

    class _Probe(CliffracerService):
        def __init__(self, config):
            super().__init__(config)
            self.seen: list[str] = []

        @listener("events.thing.happened", fanout=True)
        async def on_thing(
            self,
            subject: str,
            document_id: str = "",
            correlation_id: str | None = None,
        ) -> None:
            self.seen.append(CorrelationContext.get())

    svc = _Probe(ServiceConfig(name="svc", nats_url="nats://localhost:4222"))
    svc._discover_handlers()
    return svc


def test_two_id_less_events_get_different_correlation_ids():
    """Ensure two distinct messages without IDs receive different correlation IDs."""
    svc = _service()

    async def run():
        await svc.container._dispatch_event(_Msg(b'{"document_id": "one"}'))
        await svc.container._dispatch_event(_Msg(b'{"document_id": "two"}'))

    asyncio.run(run())

    assert len(svc.seen) == 2, svc.seen
    assert svc.seen[0] is not None and svc.seen[1] is not None, svc.seen
    assert svc.seen[0] != svc.seen[1], (
        f"both events were stamped {svc.seen[0]} -- the second inherited the "
        f"first message's id from the context variable"
    )


def test_an_event_carrying_a_correlation_id_keeps_it():
    """Ensure an event carrying an explicit correlation ID preserves it."""
    svc = _service()
    asyncio.run(svc.container._dispatch_event(_Msg(b'{"correlation_id": "corr_frompublisher"}')))
    assert svc.seen == ["corr_frompublisher"], svc.seen


def test_the_context_is_restored_after_dispatch():
    """Ensure context is restored to its pre-dispatch state after execution."""
    svc = _service()

    async def run():
        token = correlation_id_var.set("corr_beforedispatch")
        try:
            await svc.container._dispatch_event(_Msg(b'{"document_id": "one"}'))
            return correlation_id_var.get()
        finally:
            correlation_id_var.reset(token)

    after = asyncio.run(run())
    assert svc.seen == [svc.seen[0]] and svc.seen[0] != "corr_beforedispatch", (
        f"the handler was stamped {svc.seen!r} -- it inherited the ambient id "
        f"instead of getting one of its own"
    )
    assert after == "corr_beforedispatch", (
        f"the context was left holding {after!r}; the next message on this "
        f"callback would inherit it"
    )


def test_the_context_is_restored_even_when_the_handler_raises():
    """Ensure context is restored even when a handler raises an exception."""
    from cliffracer import CliffracerService, ServiceConfig, listener

    seen_id: list[str] = []

    class _Boom(CliffracerService):
        @listener("events.thing.happened", fanout=True)
        async def on_thing(
            self,
            subject: str,
            document_id: str = "",
            correlation_id: str | None = None,
        ) -> None:
            seen_id.append(CorrelationContext.get())
            raise RuntimeError("handler failed")

    svc = _Boom(ServiceConfig(name="boom", nats_url="nats://localhost:4222"))
    svc._discover_handlers()

    async def run():
        token = correlation_id_var.set("corr_beforedispatch")
        try:
            await svc.container._dispatch_event(_Msg(b'{"document_id": "one"}'))
            return correlation_id_var.get()
        finally:
            correlation_id_var.reset(token)

    after = asyncio.run(run())
    assert after == "corr_beforedispatch", (
        f"the context was left holding {after!r} after the handler raised"
    )
    assert seen_id and seen_id[0] != "corr_beforedispatch", (
        f"the handler was stamped {seen_id!r} -- it inherited the ambient id, "
        f"so this test would pass on the old code without the reset existing"
    )
