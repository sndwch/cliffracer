"""`InMemoryBroker` does what its docstring and the api reference say, where no broker can check it.

The behaviours a real broker shares are cases in `tests/contract/transport_cases.py`, run against
both. What is here belongs to the in-memory side alone: waiting for its deliveries, recording what
went over it, refusing what it does not model, and ignoring the URL a dial passes.
"""

import asyncio
import dataclasses
from collections.abc import AsyncIterator

import nats.errors
import pytest

import cliffracer.testing
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import InMemoryBroker, ServiceTestHarness

pytestmark = pytest.mark.unit


@pytest.fixture
async def broker():
    """A broker whose connections are closed when the test ends, so no delivery task outlives it."""
    under_test = InMemoryBroker()
    opened = []
    connect = under_test.connect

    async def connect_and_keep(*args, **kwargs):
        opened.append(await connect(*args, **kwargs))
        return opened[-1]

    under_test.connect = connect_and_keep  # type: ignore[method-assign]
    yield under_test
    for conn in opened:
        await conn.close()


async def test_settle_returns_once_every_queued_delivery_has_run(broker):
    """A delivery runs on a task after `publish` returns; `settle()` waits for it."""
    conn = await broker.connect()
    handled: list[bytes] = []

    async def slow(msg):
        await asyncio.sleep(0.01)
        handled.append(msg.data)

    await conn.subscribe("work", cb=slow)
    for n in range(3):
        await conn.publish("work", str(n).encode())
    assert handled == [], "a delivery ran inside publish, before it returned"

    await broker.settle()

    assert handled == [b"0", b"1", b"2"]


async def test_settle_fails_by_name_on_a_callback_that_never_returns(broker):
    """A stuck callback ends the wait with an error naming what is still in flight."""
    conn = await broker.connect()
    release = asyncio.Event()

    async def stuck(msg):
        await release.wait()

    await conn.subscribe("stuck", cb=stuck)
    await conn.publish("stuck", b"")
    try:
        with pytest.raises(AssertionError, match=r"did not settle within 0\.2s: 1 deliveries"):
            await broker.settle(timeout=0.2)
    finally:
        release.set()
        await broker.settle()


async def test_what_a_callback_raised_is_kept_in_order(broker):
    """`handler_errors` holds each exception a callback raised, in the order they were raised."""
    conn = await broker.connect()

    async def raises(msg):
        raise ValueError(msg.data.decode())

    await conn.subscribe("bad", cb=raises)
    await conn.publish("bad", b"one")
    await conn.publish("bad", b"two")
    await broker.settle()

    assert [str(e) for e in broker.handler_errors] == ["one", "two"]


async def test_jetstream_is_refused_by_name(broker):
    """JetStream, and so KV, is not modelled: asking for it says so instead of answering."""
    conn = await broker.connect()

    with pytest.raises(NotImplementedError, match="does not model JetStream"):
        conn.jetstream()


async def test_the_url_a_dial_passes_is_not_read(broker):
    """The broker is the one `connect` is called on, whatever URL and dial options come with it."""
    seen: list[bytes] = []
    listener = await broker.connect("nats://nowhere.invalid:1", connect_timeout=0.001)
    sender = await broker.connect("nats://elsewhere.invalid:2", max_reconnect_attempts=0)

    await listener.subscribe("ping", cb=lambda msg: seen.append(msg.data))
    await sender.publish("ping", b"same broker")
    await broker.settle()

    assert seen == [b"same broker"]


async def test_the_records_are_read_only_and_keep_what_was_sent(broker):
    """A record cannot be changed, and changing what was sent afterwards does not change it."""
    conn = await broker.connect()
    headers = {"X-Sent": "as sent"}

    await conn.subscribe("rec", queue="q", cb=lambda msg: None)
    await conn.publish("rec", b"data", headers=headers)
    headers["X-Sent"] = "changed after"
    await broker.settle()

    (sent,) = broker.published
    assert (sent.subject, sent.data, dict(sent.headers or {}), sent.reply) == (
        "rec",
        b"data",
        {"X-Sent": "as sent"},
        None,
    )
    with pytest.raises(dataclasses.FrozenInstanceError):
        sent.subject = "other"  # type: ignore[misc]
    with pytest.raises(TypeError):
        sent.headers["X-Sent"] = "rewritten"  # type: ignore[index]
    assert isinstance(broker.published, tuple)
    assert isinstance(broker.subscribed, tuple)
    assert [(s.subject, s.queue) for s in broker.subscribed] == [("rec", "q")]


async def test_drain_runs_what_each_subscription_holds_before_it_closes(broker):
    """`drain` hands every queued message to its callback, then closes the connection."""
    sender, receiver = await broker.connect(), await broker.connect()
    handled: list[bytes] = []

    await receiver.subscribe("drain", cb=lambda msg: handled.append(msg.data))
    for n in range(3):
        await sender.publish("drain", str(n).encode())

    await receiver.drain()

    assert handled == [b"0", b"1", b"2"]
    assert receiver.is_closed


async def test_a_request_leaves_no_subscription_behind_whether_answered_or_not(broker):
    """A request's reply inbox is the request's own: answered, unanswered or refused, it is gone
    when the request returns, so a connection that makes requests does not grow subscriptions."""
    caller, responder = await broker.connect(), await broker.connect()

    async def answer(msg):
        await msg.respond(b"pong")

    await responder.subscribe("asked", cb=answer)
    await responder.subscribe("ignored", cb=lambda msg: None)
    before = caller.subscriptions

    await caller.request("asked", b"ping", timeout=5.0)
    with pytest.raises(nats.errors.TimeoutError):
        await caller.request("ignored", b"ping", timeout=0.05)
    with pytest.raises(nats.errors.NoRespondersError):
        await caller.request("nobody", b"ping", timeout=5.0)

    assert caller.subscriptions == before == ()


async def test_the_connection_class_is_not_exported(broker):
    """Connections come from `broker.connect()`. The class is private and `cliffracer.testing`
    hands it out under no name, so nothing builds one without a broker."""
    conn = await broker.connect()
    connection_class = type(conn)

    assert connection_class.__name__.startswith("_")
    assert connection_class not in vars(cliffracer.testing).values()
    assert connection_class.__name__ not in cliffracer.testing.__all__
    assert "InMemoryBroker" in cliffracer.testing.__all__


async def _replies_under(broker, response_max, user="feeder"):
    """A responder dialled as `user` publishes three replies to one request; return what the
    caller's inbox received and what the responder's error_cb was handed."""
    if response_max is not None:
        broker.allow_responses("feeder", response_max)
    errors: list[Exception] = []

    async def keep(error):
        errors.append(error)

    responder = await broker.connect(user=user, error_cb=keep)
    caller = await broker.connect()
    got: list[bytes] = []

    async def answer(msg):
        for n in range(3):
            await responder.publish(msg.reply, str(n).encode())

    async def arrive(msg):
        got.append(msg.data)

    await responder.subscribe("ask", cb=answer)
    await caller.subscribe("_INBOX.caller", cb=arrive)
    await caller.publish("ask", b"", reply="_INBOX.caller")
    await broker.settle()
    return got, errors


async def test_a_response_grant_of_one_delivers_the_first_reply_and_refuses_the_rest(broker):
    got, errors = await _replies_under(broker, 1)

    assert got == [b"0"]
    assert [str(e) for e in errors] == [
        'nats: permissions violation for publish to "_inbox.caller"'
    ] * 2
    assert all(type(e) is nats.errors.Error for e in errors)
    assert [p.data for p in broker.published if p.subject == "_INBOX.caller"] == [b"0"]


@pytest.mark.parametrize(
    ("response_max", "user"),
    [
        pytest.param(-1, "feeder", id="no-limit"),
        pytest.param(3, "feeder", id="exactly-enough"),
        pytest.param(None, "feeder", id="no-grant-at-all"),
        pytest.param(1, "someone-else", id="another-users-grant"),
    ],
)
async def test_replies_within_the_grant_or_with_none_all_arrive(broker, response_max, user):
    got, errors = await _replies_under(broker, response_max, user=user)

    assert (got, errors) == ([b"0", b"1", b"2"], [])


@pytest.mark.parametrize("count", [0, -2, True])
def test_a_reply_count_a_broker_would_read_otherwise_is_refused(count):
    with pytest.raises(ValueError, match="response_max must be -1"):
        InMemoryBroker().allow_responses("feeder", count)


async def test_a_streaming_service_under_a_grant_of_one_is_cut_after_its_first_item(broker):
    """What a secured broker does to a service whose role grants one reply: the caller gets the
    first item and then nothing, and the service is told each later one was refused."""

    class Feeds(CliffracerService):
        @rpc
        async def tail(self, n: int) -> AsyncIterator[int]:
            for value in range(n):
                yield value

    refused: list[Exception] = []

    async def keep(error):
        refused.append(error)

    broker.allow_responses("feeder", 1)
    config = ServiceConfig(
        name="feeds",
        subject_prefix=None,
        health_port=0,
        nats_user="feeder",
        nats_password="disposable",
        on_error=keep,
    )
    caller = await broker.connect()
    got = []

    async def arrive(msg):
        got.append(msg)

    async with ServiceTestHarness(Feeds(config), broker=broker):
        await caller.subscribe("_INBOX.reader", cb=arrive)
        await caller.publish(
            "feeds.rpc.tail",
            b'{"n": 3}',
            reply="_INBOX.reader",
            headers={"Content-Type": "application/json", "Cliffracer-Stream": "1"},
        )
        await broker.settle()

    assert [m.headers.get("Cliffracer-Stream-Seq") for m in got] == ["0"]
    assert len(refused) == 3, refused
    assert all("permissions violation for publish" in str(e) for e in refused)


async def _two_asks(broker, inboxes):
    """A responder granted one reply answers two replies to each of two requests, one per
    entry of `inboxes`; return what arrived on each inbox."""
    broker.allow_responses("feeder", 1)
    responder = await broker.connect(user="feeder", error_cb=_ignore)
    caller = await broker.connect()
    got: dict[str, list[bytes]] = {inbox: [] for inbox in inboxes}

    async def answer(msg):
        for n in range(2):
            await responder.publish(msg.reply, msg.data + str(n).encode())

    def arrive(inbox):
        async def keep(msg):
            got[inbox].append(msg.data)

        return keep

    await responder.subscribe("ask", cb=answer)
    for inbox in set(inboxes):
        await caller.subscribe(inbox, cb=arrive(inbox))
    for n, inbox in enumerate(inboxes):
        await caller.publish("ask", f"r{n}-".encode(), reply=inbox)
        await broker.settle()
    return got


async def _ignore(_error):
    return None


async def test_a_grant_counts_each_request_on_its_own(broker):
    """A service granted one reply answers every request once, not only its first."""
    got = await _two_asks(broker, ["_INBOX.first", "_INBOX.second"])

    assert got == {"_INBOX.first": [b"r0-0"], "_INBOX.second": [b"r1-0"]}


async def test_a_request_delivered_again_on_the_same_reply_subject_starts_a_fresh_count(broker):
    """As the broker does: the second delivery grants its own reply, whatever the first spent."""
    got = await _two_asks(broker, ["_INBOX.again", "_INBOX.again"])

    assert got == {"_INBOX.again": [b"r0-0", b"r1-0"]}
