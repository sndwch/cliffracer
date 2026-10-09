"""An id a service sends is held to the rule an inbound id is: printable text of at most 256 characters.

The receiver refuses an id that holds a control character or is longer than the bound, starts a
new trace and logs two warnings, so a sender that put such an id on a message believed it was
continuing a trace the receiver had dropped, and wrote the id raw into its own log: a newline
in it forged a line. The id came from the caller (`correlation_id=`), from the headers of a
`ServiceClient`, or from the ambient `CorrelationContext`, which `set()` fills without a check.
Every send now takes its id from the one function that applies the rule: an id that fails it is
treated as absent, as it is on the way in, and the next source is tried.
"""

import json
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.client import ServiceClient
from cliffracer.core.correlation import MAX_CORRELATION_ID_LENGTH, CorrelationContext

pytestmark = pytest.mark.unit

TOO_LONG = "x" * (MAX_CORRELATION_ID_LENGTH + 1)
FORGED = "evil\nINFO forged log line"
UNUSABLE = [
    pytest.param(FORGED, id="newline"),
    pytest.param("esc\x1b[2J", id="escape"),
    pytest.param(TOO_LONG, id="over-the-bound"),
]


class Svc(CliffracerService):
    def __init__(self) -> None:
        super().__init__(ServiceConfig(name="orders", subject_prefix=None, health_port=0))


class Wire:
    """A connection that records the headers and the body of every message sent."""

    is_closed = False

    def __init__(self) -> None:
        self.sent: list[tuple[dict, bytes]] = []

    async def request(self, subject, payload, timeout=None, headers=None):
        self.sent.append((dict(headers or {}), payload))
        reply = AsyncMock()
        reply.data = b'{"success": true, "result": 1}'
        reply.headers = None
        return reply

    async def publish(self, subject, payload, headers=None):
        self.sent.append((dict(headers or {}), payload))


# Each sends one message through one API, with `given` as the caller's own id when it is not None.
SENDERS = {
    "call_rpc": lambda s, given: s.call_rpc(
        "billing", "charge", **({"correlation_id": given} if given is not None else {})
    ),
    "call_async": lambda s, given: s.call_async(
        "billing", "charge", **({"correlation_id": given} if given is not None else {})
    ),
    "call_rpc_no_wait": lambda s, given: s.call_rpc_no_wait(
        "billing", "charge", **({"correlation_id": given} if given is not None else {})
    ),
    "publish_event": lambda s, given: s.publish_event(
        "order.created", **({"correlation_id": given} if given is not None else {}), amount=1
    ),
    "broadcast_message": lambda s, given: s.broadcast_message(
        "order.alert", **({"correlation_id": given} if given is not None else {}), amount=1
    ),
}
SENDER_IDS = list(SENDERS)


def _ids_on(message: tuple[dict, bytes]) -> set[str]:
    """Every id the message carries: the headers and the envelope or the payload."""
    headers, body = message
    found = {v for k, v in headers.items() if k.lower() in {"x-correlation-id", "correlation_id"}}
    decoded = json.loads(body)
    for holder in (decoded, decoded.get("data") if isinstance(decoded, dict) else None):
        if isinstance(holder, dict) and isinstance(holder.get("correlation_id"), str):
            found.add(holder["correlation_id"])
    return found


@pytest.fixture
def log():
    lines: list[str] = []
    handler = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
    try:
        yield lines
    finally:
        logger.remove(handler)


@pytest.fixture(autouse=True)
def _no_ambient_id():
    CorrelationContext.clear()
    yield
    CorrelationContext.clear()


async def _send(sender: str, given: str | None = None) -> tuple[Wire, tuple[dict, bytes]]:
    service = Svc()
    wire = Wire()
    service.nc = wire
    await SENDERS[sender](service, given)
    return wire, wire.sent[-1]


@pytest.mark.parametrize("sender", SENDER_IDS)
@pytest.mark.parametrize("bad", UNUSABLE)
async def test_an_unusable_id_given_by_the_caller_is_not_sent_or_logged(sender, bad, log):
    _, message = await _send(sender, bad)

    ids = _ids_on(message)
    assert ids and bad not in ids and all(i.isprintable() and len(i) <= 256 for i in ids), ids
    assert len(ids) == 1, "the header and the body carry one id"
    assert not any("\n" in line for line in log)
    assert not any(bad in line for line in log)


@pytest.mark.parametrize("sender", SENDER_IDS)
@pytest.mark.parametrize("bad", UNUSABLE)
async def test_an_unusable_ambient_id_is_not_sent_or_logged(sender, bad, log):
    CorrelationContext.set(bad)

    _, message = await _send(sender)

    ids = _ids_on(message)
    assert ids and bad not in ids and all(i.isprintable() and len(i) <= 256 for i in ids), ids
    assert not any("\n" in line for line in log)


@pytest.mark.parametrize("sender", SENDER_IDS)
async def test_CONTROL_a_usable_id_given_by_the_caller_is_sent_as_given(sender):
    _, message = await _send(sender, "trace-from-the-gateway")

    assert _ids_on(message) == {"trace-from-the-gateway"}


@pytest.mark.parametrize("sender", SENDER_IDS)
async def test_CONTROL_a_usable_ambient_id_is_the_one_sent(sender):
    CorrelationContext.set("ambient-trace")

    _, message = await _send(sender)

    assert _ids_on(message) == {"ambient-trace"}


@pytest.mark.parametrize("sender", SENDER_IDS)
async def test_CONTROL_with_no_id_anywhere_a_new_one_is_made(sender):
    _, message = await _send(sender)

    (new_id,) = _ids_on(message)
    assert new_id.startswith("corr_")


@pytest.mark.parametrize("sender", SENDER_IDS)
async def test_the_bound_is_inclusive(sender):
    at_the_bound = "y" * MAX_CORRELATION_ID_LENGTH

    _, message = await _send(sender, at_the_bound)

    assert _ids_on(message) == {at_the_bound}


# The standalone client reads the same rule for the ambient id; a header id is read by 2268's
# function, which applies it.


def _client_headers() -> dict:
    client = ServiceClient(AsyncMock(), service="billing", verify=False)
    return client._headers_for_send()


@pytest.mark.parametrize("bad", UNUSABLE)
def test_the_client_does_not_send_an_unusable_ambient_id(bad, log):
    CorrelationContext.set(bad)

    headers = _client_headers()

    assert headers["X-Correlation-ID"] != bad
    assert headers["X-Correlation-ID"].isprintable() and len(headers["X-Correlation-ID"]) <= 256
    assert headers["correlation_id"] == headers["X-Correlation-ID"]
    assert not any("\n" in line for line in log)


def test_CONTROL_the_client_sends_a_usable_ambient_id():
    CorrelationContext.set("ambient-trace")

    assert _client_headers()["X-Correlation-ID"] == "ambient-trace"


@pytest.mark.parametrize("bad", UNUSABLE)
def test_the_client_does_not_send_an_unusable_header_id(bad):
    client = ServiceClient(
        AsyncMock(), service="billing", verify=False, headers={"X-Correlation-ID": bad}
    )

    sent = client._headers_for_send()["X-Correlation-ID"]

    assert sent != bad and sent.isprintable() and len(sent) <= 256
