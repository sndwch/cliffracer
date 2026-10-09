"""A failure of the connection on a send reaches the caller as an `RpcError`, whichever API sent.

The standalone `ServiceClient` maps what nats-py raises on its request path, and the API reference
says `call_rpc` raises the same classes. `call_rpc` caught a timeout, no responders and a lost
connection only, and `call_async`, `call_rpc_no_wait`, `publish_event` and `broadcast_message`
mapped nothing: an argument over the broker's `max_payload`, a send during the reconnect gap with
the buffer full, and a send while the connection drains each reached a handler's `except RpcError`
as a raw nats-py class. One function now maps them for the client and the service, so the two
cannot disagree.
"""

from unittest.mock import AsyncMock

import pytest
from nats import errors
from nats.aio.client import Client

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcClientError, RpcConnectionError, RpcError

pytestmark = pytest.mark.unit


class Svc(CliffracerService):
    def __init__(self) -> None:
        super().__init__(
            ServiceConfig(name="orders", subject_prefix=None, health_port=0, request_timeout=0.05)
        )


SENDERS = {
    "call_rpc": lambda s: s.call_rpc("billing", "charge", amount=1),
    "call_async": lambda s: s.call_async("billing", "charge", amount=1),
    "call_rpc_no_wait": lambda s: s.call_rpc_no_wait("billing", "charge", amount=1),
    "publish_event": lambda s: s.publish_event("order.created", amount=1),
    "broadcast_message": lambda s: s.broadcast_message("order.alert", amount=1),
}
SENDER_IDS = list(SENDERS)

# What a connection raises, the class the caller meets, and what the message says.
MAPPED = [
    pytest.param(errors.MaxPayloadError(), RpcClientError, "max_payload", id="over-max-payload"),
    pytest.param(
        errors.OutboundBufferLimitError(), RpcConnectionError, "buffer is full", id="buffer-full"
    ),
    pytest.param(errors.ConnectionDrainingError(), RpcConnectionError, "draining", id="draining"),
    pytest.param(errors.ConnectionClosedError(), RpcConnectionError, "connection", id="closed"),
    pytest.param(errors.StaleConnectionError(), RpcConnectionError, "connection", id="stale"),
]


def _service(raised: BaseException) -> Svc:
    service = Svc()
    nc = AsyncMock()
    nc.is_closed = False
    nc.request.side_effect = raised
    nc.publish.side_effect = raised
    service.nc = nc
    return service


@pytest.mark.parametrize("sender", SENDER_IDS)
@pytest.mark.parametrize(("raised", "expected", "says"), MAPPED)
async def test_a_nats_error_on_a_send_is_an_rpc_error_of_the_documented_class(
    sender, raised, expected, says
):
    service = _service(raised)

    with pytest.raises(RpcError) as caught:
        await SENDERS[sender](service)

    assert type(caught.value) is expected
    assert says in str(caught.value)
    assert caught.value.__cause__ is raised, "the nats-py error is kept as the cause"


@pytest.mark.parametrize("sender", SENDER_IDS)
async def test_an_oversized_argument_is_the_callers_and_not_a_connection_failure(sender):
    service = _service(errors.MaxPayloadError())

    with pytest.raises(RpcClientError) as caught:
        await SENDERS[sender](service)

    assert not isinstance(caught.value, RpcConnectionError)


@pytest.mark.parametrize(("raised", "expected", "says"), MAPPED)
async def test_the_client_and_the_service_raise_the_same_class_for_the_same_failure(
    raised, expected, says
):
    client = ServiceClient(service="billing", verify=False)
    client._nc = AsyncMock()
    client._nc.request.side_effect = raised
    service = _service(raised)

    with pytest.raises(RpcError) as from_client:
        await client._request("billing.rpc.charge", b"{}")
    with pytest.raises(RpcError) as from_service:
        await service.call_rpc("billing", "charge", amount=1)

    assert type(from_client.value) is type(from_service.value) is expected


# The same three failures, raised by nats-py itself from a connection forced into each state.


def _real_connection(state: str) -> Client:
    nc = Client()
    nc._status = {
        "over-max-payload": Client.CONNECTED,
        "buffer-full": Client.RECONNECTING,
        "draining": Client.DRAINING_PUBS,
    }[state]
    nc._max_payload = 8 if state == "over-max-payload" else 1 << 20
    nc._max_pending_size = 10 if state == "buffer-full" else 2 * 1024 * 1024
    nc._resp_sub_prefix = bytearray(b"_INBOX.x.")
    return nc


@pytest.mark.parametrize("sender", SENDER_IDS)
@pytest.mark.parametrize(
    ("state", "expected"),
    [
        pytest.param("over-max-payload", RpcClientError, id="over-max-payload"),
        pytest.param("buffer-full", RpcConnectionError, id="buffer-full"),
        pytest.param("draining", RpcConnectionError, id="draining"),
    ],
)
async def test_the_errors_nats_py_raises_for_itself_are_mapped(sender, state, expected):
    service = Svc()
    service.nc = _real_connection(state)

    with pytest.raises(RpcError) as caught:
        await SENDERS[sender](service)

    assert type(caught.value) is expected
    assert isinstance(caught.value.__cause__, errors.Error)


@pytest.mark.parametrize("sender", SENDER_IDS)
async def test_CONTROL_a_send_that_succeeds_raises_nothing(sender):
    service = Svc()
    nc = AsyncMock()
    nc.is_closed = False
    nc.request.return_value.data = b'{"success": true, "result": 1}'
    nc.request.return_value.headers = None
    service.nc = nc

    await SENDERS[sender](service)


async def test_CONTROL_a_timeout_is_still_a_timeout_and_no_responders_still_no_responders():
    from cliffracer.core.exceptions import RpcNoRespondersError, RpcTimeoutError

    with pytest.raises(RpcTimeoutError):
        await _service(errors.TimeoutError()).call_rpc("billing", "charge")
    with pytest.raises(RpcNoRespondersError):
        await _service(errors.NoRespondersError()).call_rpc("billing", "charge")


async def test_call_rpc_maps_any_other_nats_error_on_the_request_path_as_the_client_does():
    raised = errors.BadSubjectError()
    client = ServiceClient(service="billing", verify=False)
    client._nc = AsyncMock()
    client._nc.request.side_effect = raised

    with pytest.raises(RpcError) as from_client:
        await client._request("billing.rpc.charge", b"{}")
    with pytest.raises(RpcError) as from_service:
        await _service(raised).call_rpc("billing", "charge")

    assert type(from_client.value) is type(from_service.value) is RpcConnectionError
    assert "BadSubjectError" in str(from_service.value)


@pytest.mark.parametrize("sender", ["call_async", "call_rpc_no_wait", "publish_event"])
async def test_CONTROL_an_error_outside_the_connections_own_is_not_mapped_on_a_publish(sender):
    """A publish maps what the connection raises; other errors reach the caller as they are."""
    raised = errors.BadSubjectError()

    with pytest.raises(errors.BadSubjectError) as caught:
        await SENDERS[sender](_service(raised))

    assert caught.value is raised


def _jetstream_service(raised: BaseException) -> CliffracerService:
    from cliffracer import StreamSpec

    class StreamingSvc(CliffracerService):
        def __init__(self) -> None:
            super().__init__(
                ServiceConfig(
                    name="orders",
                    subject_prefix=None,
                    health_port=0,
                    jetstream_enabled=True,
                    jetstream_streams=[StreamSpec(name="ORDERS", subjects=["order.>"])],
                )
            )

    service = StreamingSvc()
    nc = AsyncMock()
    nc.is_closed = False
    service.nc = nc
    service.js = AsyncMock()
    service.js.publish.side_effect = raised
    return service


@pytest.mark.parametrize(("raised", "expected", "says"), MAPPED)
async def test_a_jetstream_publish_maps_the_connections_errors_too(raised, expected, says):
    service = _jetstream_service(raised)

    with pytest.raises(RpcError) as caught:
        await service.publish_event("order.created", amount=1)

    assert type(caught.value) is expected and says in str(caught.value)


async def test_CONTROL_a_jetstream_error_about_the_stream_is_not_mapped():
    from nats.js import errors as js_errors

    raised = js_errors.NoStreamResponseError()

    with pytest.raises(js_errors.NoStreamResponseError) as caught:
        await _jetstream_service(raised).publish_event("order.created", amount=1)

    assert caught.value is raised
