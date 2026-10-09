"""The behaviours the in-memory broker must share with a real NATS client.

Each case here runs against both backends: `cliffracer.testing.InMemoryBroker`
under `tests/transport/`, and a real `nats-py` client under `tests/integration/`.
Holding the list in one module is what stops a case existing for one backend
only: an in-memory broker asserted against itself agrees with itself.

A case asserts the **real client's** behaviour. Where the in-memory broker
disagrees, the broker is corrected; a case it cannot satisfy is a bug in the
broker, not a reason to weaken the case.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import nats.errors

# A request that nothing answers resolves far faster than its timeout, because
# the server says so rather than the client giving up. The fraction is loose on
# purpose: the real answer is sub-millisecond and the wrong answer is the whole
# timeout, so anything between them separates the two even on a loaded host.
RESOLVES_WITHIN = 0.5

REQUEST_TIMEOUT = 2.0

# How long a case waits for a delivery it expects before failing by name.
DELIVERED_WITHIN = 5.0

# How long a case watches for a delivery it does NOT expect, once every expected one has arrived. A
# duplicate or stray delivery on loopback arrives within a millisecond of the expected ones; the
# window is two orders above that, and a case that relies on it says so.
QUIET_FOR = 0.25

# Messages a queue-group case publishes. With two members and a broker that picks one at random,
# the chance one member gets none of them is 2 * 0.5**20, about 2 in a million.
QUEUE_MESSAGES = 20


@dataclass(frozen=True)
class Backend:
    """One transport under test, plus what its server supports."""

    name: str
    client: Any
    subject_prefix: str
    # `NoRespondersError` is a protocol feature the client enables only when the
    # server advertises header support. Against a server without it, a real
    # client times out like a naive fake -- so a case that needs it says so
    # rather than quietly passing for the wrong reason.
    supports_no_responders: bool
    #: Another connection to the same broker, closed by the leg when the case ends. Takes the
    #: connection callbacks `nats.connect` takes (`error_cb`, `closed_cb`, ...).
    connect: Callable[..., Awaitable[Any]]


@dataclass(frozen=True)
class Case:
    """One behaviour both backends must agree on."""

    name: str
    run: Callable[[Backend], Awaitable[None]]


async def request_with_no_responder_raises_and_does_not_wait(backend: Backend) -> None:
    """A request nothing answers raises NoRespondersError, well inside the timeout."""
    assert backend.supports_no_responders, (
        f"{backend.name} does not advertise header support, so no-responders is off "
        "and this case would pass by timing out, which is the behaviour it exists to reject"
    )
    subject = f"{backend.subject_prefix}.nobody.listens"

    started = time.perf_counter()
    try:
        await backend.client.request(subject, b"{}", timeout=REQUEST_TIMEOUT)
    except nats.errors.NoRespondersError:
        elapsed = time.perf_counter() - started
    else:
        raise AssertionError(
            f"{backend.name}: a request with no responder returned instead of raising"
        )

    # Upper bound. CI p99 0.00127 s (run 4712: eric-7, CPython 3.12.15, n=40, p99 = max); 784x p99;
    # below 2 s (REQUEST_TIMEOUT waited out).
    assert elapsed < REQUEST_TIMEOUT * RESOLVES_WITHIN, (
        f"{backend.name}: raised NoRespondersError only after {elapsed:.3f}s of a "
        f"{REQUEST_TIMEOUT}s timeout, so it waited the request out rather than being told"
    )


async def publishing_on_a_closed_connection_raises(backend: Backend) -> None:
    """Publishing after close raises ConnectionClosedError."""
    await backend.client.close()
    try:
        await backend.client.publish(f"{backend.subject_prefix}.after.close", b"{}")
    except nats.errors.ConnectionClosedError:
        return
    raise AssertionError(f"{backend.name}: publish on a closed connection did not raise")


async def flushing_a_closed_connection_raises(backend: Backend) -> None:
    """Flushing after close raises ConnectionClosedError."""
    await backend.client.close()
    try:
        await backend.client.flush()
    except nats.errors.ConnectionClosedError:
        return
    raise AssertionError(f"{backend.name}: flush on a closed connection did not raise")


async def subscribed(conn: Any, subject: str, queue: str = "") -> tuple[Any, list[Any]]:
    """Subscribe `conn` to `subject` and return the subscription and what it is handed, in order.

    The SUB is confirmed before this returns: one flush writes it out, the next round-trips the
    server behind it, so a publish from another connection after this cannot overtake it.
    """
    seen: list[Any] = []

    async def record(msg: Any) -> None:
        seen.append(msg)

    sub = await conn.subscribe(subject, queue=queue, cb=record)
    await conn.flush()
    await conn.flush()
    return sub, seen


async def until(condition: Callable[[], bool], what: str) -> None:
    """Wait for `condition`, bounded, so a miss fails by name instead of hanging."""
    deadline = time.monotonic() + DELIVERED_WITHIN
    while not condition():
        if time.monotonic() > deadline:
            raise AssertionError(f"timed out after {DELIVERED_WITHIN}s waiting for {what}")
        await asyncio.sleep(0.005)


async def a_published_message_arrives_with_its_payload_and_headers(backend: Backend) -> None:
    """A subscriber on another connection is handed the payload and the headers as sent."""
    sender, receiver = backend.client, await backend.connect()
    subject = f"{backend.subject_prefix}.plain"
    _, seen = await subscribed(receiver, subject)

    await sender.publish(subject, b"payload", headers={"X-Case": "headers-kept"})
    await until(lambda: len(seen) == 1, "the published message")

    (msg,) = seen
    assert (msg.subject, msg.data) == (subject, b"payload"), (backend.name, msg.subject, msg.data)
    assert msg.headers == {"X-Case": "headers-kept"}, (backend.name, msg.headers)


async def a_message_sent_without_headers_arrives_without_them(backend: Backend) -> None:
    """A message published with no headers has none: nats-py hands `headers` as None."""
    sender, receiver = backend.client, await backend.connect()
    subject = f"{backend.subject_prefix}.bare"
    _, seen = await subscribed(receiver, subject)

    await sender.publish(subject, b"x")
    await until(lambda: len(seen) == 1, "the published message")

    assert seen[0].headers is None, (backend.name, seen[0].headers)


async def a_star_matches_exactly_one_token(backend: Backend) -> None:
    """`*` stands for one whole token: not none, not two.

    The subjects go out from one connection to one subscription, which the server and the client
    both keep in order, so the last one arriving means none sent before it is still in flight.
    """
    sender, receiver = backend.client, await backend.connect()
    p = backend.subject_prefix
    _, seen = await subscribed(receiver, f"{p}.star.*.end")

    for subject in (
        f"{p}.star.end",
        f"{p}.star.a.b.end",
        f"{p}.star.one.end",
        f"{p}.star.last.end",
    ):
        await sender.publish(subject, b"")
    await until(lambda: any(m.subject == f"{p}.star.last.end" for m in seen), "the last subject")

    assert [m.subject for m in seen] == [f"{p}.star.one.end", f"{p}.star.last.end"], (
        backend.name,
        [m.subject for m in seen],
    )


async def a_trailing_gt_matches_one_or_more_tokens(backend: Backend) -> None:
    """`>` at the end stands for one or more tokens, never for none."""
    sender, receiver = backend.client, await backend.connect()
    p = backend.subject_prefix
    _, seen = await subscribed(receiver, f"{p}.tail.>")

    for subject in (f"{p}.tail", f"{p}.tail.a", f"{p}.tail.a.b.c"):
        await sender.publish(subject, b"")
    await until(lambda: any(m.subject == f"{p}.tail.a.b.c" for m in seen), "the deepest subject")

    assert [m.subject for m in seen] == [f"{p}.tail.a", f"{p}.tail.a.b.c"], (
        backend.name,
        [m.subject for m in seen],
    )


async def a_token_is_matched_as_written_whatever_characters_it_holds(backend: Backend) -> None:
    """Characters that mean something to a regular expression are plain characters in a subject."""
    sender, receiver = backend.client, await backend.connect()
    p = backend.subject_prefix
    _, dollar = await subscribed(receiver, f"{p}.$lit.*")
    _, plus = await subscribed(receiver, f"{p}.a+b")

    await sender.publish(f"{p}.aab", b"")
    await sender.publish(f"{p}.$lit.x", b"")
    await sender.publish(f"{p}.a+b", b"")
    await until(lambda: len(dollar) == 1 and len(plus) == 1, "the two literal subjects")
    await asyncio.sleep(QUIET_FOR)

    assert [m.subject for m in dollar] == [f"{p}.$lit.x"], (backend.name, dollar)
    assert [m.subject for m in plus] == [f"{p}.a+b"], (backend.name, [m.subject for m in plus])


async def a_queue_group_hands_each_message_to_one_member(backend: Backend) -> None:
    """Two members of one queue group: each message reaches exactly one, and both get some.

    A subscriber in no group is handed every message as well. Relies on QUIET_FOR: once all
    the expected deliveries are in, the window is where a second copy would show.
    """
    sender = backend.client
    first, second, plain_conn = (
        await backend.connect(),
        await backend.connect(),
        await backend.connect(),
    )
    subject = f"{backend.subject_prefix}.work"
    _, got_first = await subscribed(first, subject, queue="workers")
    _, got_second = await subscribed(second, subject, queue="workers")
    _, got_plain = await subscribed(plain_conn, subject)

    for n in range(QUEUE_MESSAGES):
        await sender.publish(subject, str(n).encode())
    await until(
        lambda: len(got_first) + len(got_second) >= QUEUE_MESSAGES
        and len(got_plain) == QUEUE_MESSAGES,
        "every message, once to the group and once to the plain subscriber",
    )
    await asyncio.sleep(QUIET_FOR)

    to_group = sorted(int(m.data) for m in got_first + got_second)
    assert to_group == list(range(QUEUE_MESSAGES)), (backend.name, to_group)
    assert got_first and got_second, (
        f"{backend.name}: one member took all {QUEUE_MESSAGES}: "
        f"first={len(got_first)} second={len(got_second)}"
    )
    assert sorted(int(m.data) for m in got_plain) == list(range(QUEUE_MESSAGES)), backend.name


async def a_reply_carries_the_headers_its_responder_set(backend: Backend) -> None:
    """`Msg.respond` sends the message's headers, so what the responder set reaches the caller."""
    caller, responder = backend.client, await backend.connect()
    subject = f"{backend.subject_prefix}.ask"

    async def answer(msg: Any) -> None:
        msg.headers = {"Content-Type": "application/msgpack", "X-Correlation-ID": "c-1"}
        await msg.respond(b"pong")

    await responder.subscribe(subject, cb=answer)
    await responder.flush()
    await responder.flush()

    reply = await caller.request(subject, b"ping", timeout=REQUEST_TIMEOUT, headers={"In": "1"})

    assert reply.data == b"pong", (backend.name, reply.data)
    assert reply.headers == {"Content-Type": "application/msgpack", "X-Correlation-ID": "c-1"}, (
        backend.name,
        reply.headers,
    )


async def a_request_nobody_answers_times_out_as_a_nats_timeout(backend: Backend) -> None:
    """A request a subscriber receives and never answers raises `nats.errors.TimeoutError`."""
    caller, silent = backend.client, await backend.connect()
    subject = f"{backend.subject_prefix}.silent"
    _, seen = await subscribed(silent, subject)

    try:
        await caller.request(subject, b"", timeout=0.2)
    except nats.errors.TimeoutError:
        pass
    else:
        raise AssertionError(f"{backend.name}: a request nobody answered returned")
    assert len(seen) == 1, f"{backend.name}: the request did not reach the silent subscriber"


async def an_unsubscribed_subscription_is_handed_nothing_more(backend: Backend) -> None:
    """After `unsubscribe`, a subscription receives nothing; one beside it still does.

    Relies on QUIET_FOR, once the other subscription has the message.
    """
    sender, receiver = backend.client, await backend.connect()
    subject = f"{backend.subject_prefix}.unsub"
    gone, got_gone = await subscribed(receiver, subject)
    _, got_kept = await subscribed(receiver, subject)

    await gone.unsubscribe()
    await receiver.flush()
    await receiver.flush()
    await sender.publish(subject, b"after")
    await until(lambda: len(got_kept) == 1, "the subscription that stayed")
    await asyncio.sleep(QUIET_FOR)

    assert got_gone == [], (backend.name, got_gone)


async def closing_one_connection_leaves_another_delivering(backend: Backend) -> None:
    """A connection that closes takes its own subscriptions with it and nobody else's."""
    sender, leaving, staying = backend.client, await backend.connect(), await backend.connect()
    subject = f"{backend.subject_prefix}.shared"
    _, got_leaving = await subscribed(leaving, subject)
    _, got_staying = await subscribed(staying, subject)

    await leaving.close()
    await sender.publish(subject, b"after")
    await until(lambda: len(got_staying) == 1, "the connection that stayed")

    assert leaving.is_closed, backend.name
    assert got_leaving == [], (backend.name, got_leaving)


async def a_raising_callback_does_not_stop_the_next_delivery(backend: Backend) -> None:
    """A callback that raises is handed the next message all the same, and its error goes to the
    connection's `error_cb`."""
    errors: list[Exception] = []

    async def record_error(exc: Exception) -> None:
        errors.append(exc)

    sender = backend.client
    receiver = await backend.connect(error_cb=record_error)
    subject = f"{backend.subject_prefix}.raises"
    seen: list[bytes] = []

    async def first_one_raises(msg: Any) -> None:
        seen.append(msg.data)
        if msg.data == b"first":
            raise ValueError("the first message breaks the callback")

    await receiver.subscribe(subject, cb=first_one_raises)
    await receiver.flush()
    await receiver.flush()
    await sender.publish(subject, b"first")
    await sender.publish(subject, b"second")
    await until(lambda: seen == [b"first", b"second"] and errors, "both messages and the error")

    assert [type(e) for e in errors] == [ValueError], (backend.name, errors)
    assert "breaks the callback" in str(errors[0]), (backend.name, errors)


#: Replies a many-replies case publishes to one inbox. Enough that a broker reordering or dropping
#: some would show, few enough to stay far below any pending limit.
REPLIES = 50


async def many_replies_to_one_inbox_arrive_in_order_with_their_headers(backend: Backend) -> None:
    """A responder may publish many messages to one request's reply subject, and a caller that
    subscribed that inbox itself receives every one, in the order they were published, each with
    its own headers. `request()` keeps only the first reply, so a caller expecting many subscribes
    first; the responder publishes rather than calls `respond`, which sends the request's headers.
    """
    caller, responder = backend.client, await backend.connect()
    subject = f"{backend.subject_prefix}.replies.many"
    inbox = f"{backend.subject_prefix}.replies.inbox"

    async def answer(msg: Any) -> None:
        for seq in range(REPLIES):
            await responder.publish(msg.reply, str(seq).encode(), headers={"Seq": str(seq)})

    await responder.subscribe(subject, cb=answer)
    await responder.flush()
    await responder.flush()
    _, seen = await subscribed(caller, inbox)

    await caller.publish(subject, b"go", reply=inbox)
    await until(lambda: len(seen) >= REPLIES, f"{REPLIES} replies on {inbox}")
    await asyncio.sleep(QUIET_FOR)

    assert [m.data for m in seen] == [str(seq).encode() for seq in range(REPLIES)], (
        backend.name,
        [m.data for m in seen],
    )
    assert [m.headers for m in seen] == [{"Seq": str(seq)} for seq in range(REPLIES)], (
        backend.name,
        [m.headers for m in seen][:3],
    )


async def a_publish_with_a_reply_subject_nobody_holds_brings_a_503(backend: Backend) -> None:
    """A message carrying a reply subject, published to a subject nobody holds, brings a 503 back
    on that reply subject: an empty message whose only header is `Status: 503`. The server sends
    it to the publishing connection, on its own subscription to the reply subject; another
    connection holding the same subject, and a queue group holding it, get nothing. A message
    published to a subject somebody holds brings nothing back, and one published to nobody without
    a reply subject goes nowhere."""
    assert backend.supports_no_responders, (
        f"{backend.name} does not advertise header support, so no 503 is sent at all"
    )
    conn, other = backend.client, await backend.connect()
    back = f"{backend.subject_prefix}.status.back"
    held = f"{backend.subject_prefix}.status.held"
    _, returned = await subscribed(conn, back)
    _, elsewhere = await subscribed(other, back)
    _, queued = await subscribed(conn, back, queue="q")
    _, delivered = await subscribed(conn, held)

    await conn.publish(f"{backend.subject_prefix}.status.nobody", b"chunk", reply=back)
    await until(lambda: len(returned) >= 1, f"a 503 on {back}")
    await conn.publish(held, b"chunk", reply=back)
    await until(lambda: len(delivered) >= 1, f"the message on {held}")
    await conn.publish(f"{backend.subject_prefix}.status.nobody", b"no reply subject")
    await asyncio.sleep(QUIET_FOR)

    assert [(m.subject, m.data, m.headers) for m in returned] == [(back, b"", {"Status": "503"})], (
        backend.name,
        [(m.subject, m.data, m.headers) for m in returned],
    )
    assert not returned[0].reply, (backend.name, returned[0].reply)
    assert (elsewhere, queued) == ([], []), (backend.name, elsewhere, queued)


CASES: tuple[Case, ...] = (
    Case("request_with_no_responder", request_with_no_responder_raises_and_does_not_wait),
    Case("publish_on_closed_connection", publishing_on_a_closed_connection_raises),
    Case("flush_on_closed_connection", flushing_a_closed_connection_raises),
    Case("payload_and_headers", a_published_message_arrives_with_its_payload_and_headers),
    Case("no_headers_is_none", a_message_sent_without_headers_arrives_without_them),
    Case("star_is_one_token", a_star_matches_exactly_one_token),
    Case("gt_is_one_or_more_tokens", a_trailing_gt_matches_one_or_more_tokens),
    Case("tokens_are_literal", a_token_is_matched_as_written_whatever_characters_it_holds),
    Case("queue_group_once_each", a_queue_group_hands_each_message_to_one_member),
    Case("reply_headers", a_reply_carries_the_headers_its_responder_set),
    Case("request_timeout", a_request_nobody_answers_times_out_as_a_nats_timeout),
    Case("unsubscribe", an_unsubscribed_subscription_is_handed_nothing_more),
    Case("close_is_per_connection", closing_one_connection_leaves_another_delivering),
    Case("raising_callback_continues", a_raising_callback_does_not_stop_the_next_delivery),
    Case("many_replies_in_order", many_replies_to_one_inbox_arrive_in_order_with_their_headers),
    Case("no_one_holds_brings_503", a_publish_with_a_reply_subject_nobody_holds_brings_a_503),
)

CASE_NAMES: tuple[str, ...] = tuple(case.name for case in CASES)
