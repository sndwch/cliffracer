"""A request or publish through `PoolExtension` carries the correlation id of the request it serves.

`docs/correlation.md` says the id is propagated across service boundaries in NATS headers.
`ServiceClient` and `call_rpc` send it; the pool sent `headers=None`, so the service that
answered a pooled request generated a new id and the trace across the two services broke.
These tests read what the pool hands the connection, so they need no broker.
"""

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_metrics import OptimizedNATSConnection, PoolExtension

from cliffracer.core.correlation import CorrelationContext

pytestmark = pytest.mark.unit


def _extension() -> tuple[PoolExtension, SimpleNamespace]:
    conn = SimpleNamespace(
        is_closed=False,
        is_connected=True,
        request=AsyncMock(return_value="reply"),
        publish=AsyncMock(),
    )
    extension = PoolExtension()
    extension.pool = OptimizedNATSConnection(max_connections=1)
    extension.pool._connections = [conn]
    return extension, conn


@pytest.fixture(autouse=True)
def _no_ambient_id():
    token = CorrelationContext.set(None)
    yield
    CorrelationContext.clear()
    del token


async def test_a_pooled_request_sends_the_ambient_correlation_id():
    extension, conn = _extension()
    CorrelationContext.set("corr_ambient")

    await extension.request("svc.rpc.work", b"{}")

    sent = conn.request.await_args.kwargs["headers"]
    assert sent["X-Correlation-ID"] == "corr_ambient"
    assert sent["correlation_id"] == "corr_ambient"


async def test_a_pooled_publish_sends_the_ambient_correlation_id():
    extension, conn = _extension()
    CorrelationContext.set("corr_ambient")

    await extension.publish("svc.events.done", b"{}")

    sent = conn.publish.await_args.kwargs["headers"]
    assert sent["X-Correlation-ID"] == "corr_ambient"


async def test_an_id_the_caller_passes_wins_over_the_ambient_one():
    extension, conn = _extension()
    CorrelationContext.set("corr_ambient")

    await extension.request("svc.rpc.work", b"{}", headers={"X-Correlation-ID": "corr_given"})

    assert conn.request.await_args.kwargs["headers"]["X-Correlation-ID"] == "corr_given"


async def test_with_no_ambient_id_a_new_one_is_sent_each_call():
    extension, conn = _extension()

    await extension.request("svc.rpc.work", b"{}")
    await extension.request("svc.rpc.work", b"{}")

    ids = [call.kwargs["headers"]["X-Correlation-ID"] for call in conn.request.await_args_list]
    assert all(ids) and ids[0] != ids[1]


async def test_the_callers_other_headers_travel_with_it():
    extension, conn = _extension()

    await extension.request("svc.rpc.work", b"{}", headers={"X-Tenant": "t1"})

    assert conn.request.await_args.kwargs["headers"]["X-Tenant"] == "t1"


async def test_CONTROL_the_pool_itself_sends_what_it_is_given_and_nothing_else():
    """`OptimizedNATSConnection` is a transport: no headers in, no `headers=` out."""
    extension, conn = _extension()
    assert extension.pool is not None

    await extension.pool.request("svc.rpc.work", b"{}")
    await extension.pool.publish("svc.events.done", b"{}", headers={"X-Only": "this"})

    assert "headers" not in conn.request.await_args.kwargs
    assert conn.publish.await_args.kwargs["headers"] == {"X-Only": "this"}


UNUSABLE_IDS = [
    pytest.param("evil\nINFO forged log line", id="newline"),
    pytest.param("x" * 257, id="over-the-bound"),
]


@pytest.mark.parametrize("bad", UNUSABLE_IDS)
async def test_an_unusable_ambient_id_is_not_sent(bad):
    """The rule an inbound id is held to: a refused id is absent, and a new one is sent."""
    extension, conn = _extension()
    CorrelationContext.set(bad)

    await extension.request("svc.rpc.work", b"{}")

    sent = conn.request.await_args.kwargs["headers"]
    assert sent["X-Correlation-ID"] != bad
    assert sent["X-Correlation-ID"].isprintable() and len(sent["X-Correlation-ID"]) <= 256
    assert sent["correlation_id"] == sent["X-Correlation-ID"]


@pytest.mark.parametrize("spelling", ["x-request-id", "X-CORRELATION-ID", "Correlation-ID"])
async def test_an_id_the_caller_passes_under_another_spelling_is_the_one_sent(spelling):
    """The receiver reads seven names in any case; the id is found under each, and sent once."""
    extension, conn = _extension()
    CorrelationContext.set("corr_ambient")

    await extension.request("svc.rpc.work", b"{}", headers={spelling: "corr_given"})

    sent = conn.request.await_args.kwargs["headers"]
    assert sent["X-Correlation-ID"] == "corr_given"
    assert sent["correlation_id"] == "corr_given"
    ids = {
        value
        for name, value in sent.items()
        if name.lower()
        in {"x-correlation-id", "correlation_id", "correlation-id", "x-request-id", "request-id"}
    }
    assert ids == {"corr_given"}, "no header names a different id"
