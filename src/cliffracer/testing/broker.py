"""An in-process broker, so several services and clients can talk to each other in one test.

`InMemoryBroker` is one broker: a subscription table, and a record of what was published on it.
`await broker.connect()` hands out a connection, which is what a service or client holds as its
`nc`: its own subscriptions, its own closed state, the same broker. `ServiceTestHarness(service,
broker=broker)` starts a service over a connection of its own, and a client takes one through its
`nc=` argument.

What it models, each behaviour also checked against a real broker in CI:

- publish and subscribe, with headers carried as sent, and none handed as `None`;
- subject matching by token, `*` for one token and a trailing `>` for one or more, every other
  token matched as written;
- queue groups: each message goes to one member of each group, rotating, and to every subscriber
  in none;
- request and reply over an inbox, the reply carrying the headers its responder set. A request
  nothing listens on raises `NoRespondersError` at once, and one nothing answers raises
  `nats.errors.TimeoutError`;
- a message carrying a reply subject, published to a subject nobody holds, brings a 503 back on
  that reply subject: an empty message whose only header is `Status: 503`, as the server sends,
  to the publishing connection's own subscription to it outside a queue group, and to no other.
  It is the same server behaviour `request` raises `NoRespondersError` for, seen from a plain
  `publish`. A message without a reply subject, published to nothing, goes nowhere. The 503 is the
  broker's, not a publish, so it is not in `published`;
- a response grant's reply count: after `broker.allow_responses(user, response_max)`, a
  connection dialled as `user` may publish `response_max` messages to the reply subject of each
  request delivered to it (`-1` for no limit), and a publish past that is not delivered and is
  reported to its `error_cb` as `nats: permissions violation for publish to "<subject>"`, as a
  broker with `allow_responses.max` refuses it. A refused publish is not in `published`. The
  grant's expiry, and every other permission, are not modelled;
- `unsubscribe`, and `close` on one connection, which leaves every other connection delivering;
- delivery on a task per subscription, in order per subscription, so a callback runs after
  `publish` returns. A callback that raises does not stop the next delivery: its error is handed
  to the connection's `error_cb`, as a client does, and kept in `handler_errors`. `await
  broker.settle()` returns once every delivery handed out so far has run.

JetStream, and so KV and the object store, are not modelled: `jetstream()` raises
`NotImplementedError`. Run anything that needs streams against a broker.
"""

from __future__ import annotations

import asyncio
import itertools
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from types import MappingProxyType
from typing import Any

import nats.errors

from cliffracer.testing.messages import refuse_a_reply_with_no_subject

Callback = Callable[["_Message"], Any]
ConnectionCallback = Callable[..., Awaitable[None]]


@dataclass(frozen=True)
class Published:
    """One publish on the broker: what was sent, as it was sent."""

    subject: str
    data: bytes
    headers: MappingProxyType[str, str] | None
    reply: str | None


@dataclass(frozen=True)
class Subscribed:
    """One `subscribe` on the broker: the subject and the queue group it asked for."""

    subject: str
    queue: str | None


class _Message:
    """A delivered message: the part of `nats.aio.msg.Msg` a core subscriber reads.

    `respond` publishes the reply with the message's own `headers`, as `Msg.respond` does, so
    whatever the responder set on it before replying reaches the requester.
    """

    def __init__(
        self,
        connection: _Connection,
        subject: str,
        data: bytes,
        headers: dict[str, str] | None,
        reply: str | None,
    ) -> None:
        self._connection = connection
        self.subject = subject
        self.data = data
        self.headers = headers
        self.reply = reply

    async def respond(self, data: bytes) -> None:
        refuse_a_reply_with_no_subject(self)
        assert self.reply is not None
        await self._connection.publish(self.reply, data, headers=self.headers)


class _Subscription:
    """One subscription: its pattern, its queue group, and the task that runs its deliveries."""

    def __init__(
        self, connection: _Connection, subject: str, cb: Callback, queue: str | None
    ) -> None:
        self.connection = connection
        self.subject = subject
        self.queue = queue
        self._cb = cb
        self._inbox: asyncio.Queue[_Message] = asyncio.Queue()
        self._worker = asyncio.get_running_loop().create_task(
            self._run(), name=f"in-memory-broker:{subject}"
        )

    def deliver(self, msg: _Message) -> None:
        self.connection._broker._in_flight += 1
        self._inbox.put_nowait(msg)

    async def _run(self) -> None:
        broker = self.connection._broker
        while True:
            msg = await self._inbox.get()
            try:
                result = self._cb(msg)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as exc:  # noqa: BLE001 - a client hands a callback's error to error_cb
                broker._handler_errors.append(exc)
                await self.connection._report_error(exc)
            finally:
                broker._delivered()

    async def unsubscribe(self) -> None:
        self.connection._remove(self)

    def _stop(self) -> None:
        broker = self.connection._broker
        while not self._inbox.empty():
            self._inbox.get_nowait()
            broker._delivered()
        self._worker.cancel()

    async def _drained(self) -> None:
        while not self._inbox.empty():
            await asyncio.sleep(0)


class _Connection:
    """One client connection to an `InMemoryBroker`: what a service or a client holds as `nc`.

    It answers the part of `nats.aio.client.Client` the library calls: `publish`, `subscribe`,
    `new_inbox`, `request`, `flush`, `drain`, `close`, `jetstream()`, the state flags, and
    `_send_ping`, which readiness uses for a round trip.
    """

    def __init__(
        self,
        broker: InMemoryBroker,
        name: str | None = None,
        error_cb: ConnectionCallback | None = None,
        disconnected_cb: ConnectionCallback | None = None,
        closed_cb: ConnectionCallback | None = None,
        response_max: int | None = None,
    ) -> None:
        self._broker = broker
        self.name = name
        # The replies each request delivered here may have, as the user's `allow_responses.max`
        # grants them (-1 for no limit), or None for a user the broker grants none, whose
        # publishes are not limited. `_granted` holds what each reply subject has left; an entry
        # is kept for the connection's life, since no expiry is modelled to drop it, which a test's
        # few requests do not notice.
        self._response_max = response_max
        self._granted: dict[str, int] = {}
        self._error_cb = error_cb
        self._disconnected_cb = disconnected_cb
        self._closed_cb = closed_cb
        self._subscriptions: list[_Subscription] = []
        self.is_connected = True
        self.is_closed = False
        self.is_draining = False
        self.is_connecting = False
        self.is_reconnecting = False

    @property
    def broker(self) -> InMemoryBroker:
        """The broker this connection is on."""
        return self._broker

    @property
    def subscriptions(self) -> tuple[Subscribed, ...]:
        """What this connection holds now."""
        return tuple(Subscribed(sub.subject, sub.queue) for sub in self._subscriptions)

    def _require_open(self) -> None:
        if self.is_closed or not self.is_connected:
            raise nats.errors.ConnectionClosedError

    async def _report_error(self, exc: Exception) -> None:
        if self._error_cb is not None:
            await self._error_cb(exc)

    def jetstream(self, **kwargs: Any) -> Any:
        raise NotImplementedError(
            "the in-memory broker does not model JetStream, so neither KV nor the object store: "
            "run anything that needs streams against a broker"
        )

    async def publish(
        self,
        subject: str,
        payload: bytes = b"",
        reply: str = "",
        headers: dict[str, str] | None = None,
    ) -> None:
        self._require_open()
        if not self._may_reply_to(subject):
            # As the broker refuses it: not delivered, and reported to this connection alone.
            await self._report_error(
                nats.errors.Error(f'nats: permissions violation for publish to "{subject.lower()}"')
            )
            return
        self._broker._route(self, subject, payload, reply or None, headers)

    def _granted_reply(self, reply: str) -> None:
        """A request delivered here with `reply` grants that many replies to it."""
        if self._response_max is not None:
            self._granted[reply] = self._response_max

    def _may_reply_to(self, subject: str) -> bool:
        """Whether a publish to `subject` is within this user's response grant, counting it."""
        left = self._granted.get(subject)
        if left is None or left == -1:
            return True
        if left == 0:
            return False
        self._granted[subject] = left - 1
        return True

    def new_inbox(self) -> str:
        """A unique reply subject, as a client's inboxes are: `_INBOX.<id>`."""
        return f"_INBOX.{uuid.uuid4().hex}"

    async def subscribe(
        self,
        subject: str,
        queue: str = "",
        cb: Callback | None = None,
        **kwargs: Any,
    ) -> _Subscription:
        self._require_open()
        if cb is None:
            raise NotImplementedError("the in-memory broker delivers to callbacks only: pass cb")
        self._broker._subscribed.append(Subscribed(subject, queue or None))
        return self._attach(subject, cb, queue or None)

    def _attach(self, subject: str, cb: Callback, queue: str | None) -> _Subscription:
        """Add a subscription to this connection and the broker, unrecorded.

        `subscribe` records what a caller asked for; a request's own reply inbox, which the client
        sets up for itself, comes straight here.
        """
        sub = _Subscription(self, subject, cb, queue)
        self._subscriptions.append(sub)
        self._broker._subscriptions.append(sub)
        return sub

    def _remove(self, sub: _Subscription) -> None:
        if sub in self._subscriptions:
            self._subscriptions.remove(sub)
        if sub in self._broker._subscriptions:
            self._broker._subscriptions.remove(sub)
        sub._stop()

    async def request(
        self,
        subject: str,
        payload: bytes = b"",
        timeout: float = 0.5,
        old_style: bool = False,
        headers: dict[str, str] | None = None,
    ) -> _Message:
        self._require_open()
        if not self._broker._has_subscriber_for(subject):
            # A server answers a request nothing listens on at once, so the caller learns there
            # is no responder rather than waiting out its own timeout.
            raise nats.errors.NoRespondersError
        inbox = self.new_inbox()
        reply: asyncio.Future[_Message] = asyncio.get_running_loop().create_future()

        def answered(msg: _Message) -> None:
            if not reply.done():
                reply.set_result(msg)

        sub = self._attach(inbox, answered, None)
        try:
            await self.publish(subject, payload, reply=inbox, headers=headers)
            try:
                return await asyncio.wait_for(reply, timeout=timeout)
            except TimeoutError:
                raise nats.errors.TimeoutError from None
        finally:
            self._remove(sub)

    async def flush(self, timeout: float | None = None) -> None:
        """A client round-trips the server here; in memory nothing is in flight to the broker."""
        self._require_open()

    async def _send_ping(self, future: asyncio.Future[Any] | None = None) -> None:
        """The PONG a broker that is up sends: resolve the round trip's future."""
        self._require_open()
        if future is not None and not future.done():
            future.set_result(True)

    async def drain(self) -> None:
        """Run what each subscription already holds, then close."""
        self._require_open()
        self.is_draining = True
        for sub in list(self._subscriptions):
            await sub._drained()
        await self.close()

    async def close(self) -> None:
        """Close this connection: its subscriptions end, and every other connection's stay."""
        if self.is_closed:
            return
        for sub in list(self._subscriptions):
            self._remove(sub)
        self.is_draining = False
        self.is_connected = False
        self.is_closed = True
        if self._disconnected_cb is not None:
            await self._disconnected_cb()
        if self._closed_cb is not None:
            await self._closed_cb()


def _subject_matches(pattern: str, subject: str) -> bool:
    """Whether subscription `pattern` matches `subject`, token by token."""
    want = pattern.split(".")
    got = subject.split(".")
    for i, token in enumerate(want):
        if token == ">" and i == len(want) - 1:
            return len(got) > i
        if i >= len(got):
            return False
        if token != "*" and token != got[i]:
            return False
    return len(want) == len(got)


class InMemoryBroker:
    """One broker in the test process. Take a connection from it for each service and client.

    Provisional: its API may change in the next minor release without a deprecation.
    """

    def __init__(self) -> None:
        self._subscriptions: list[_Subscription] = []
        self._rotation: dict[str, itertools.count[int]] = {}
        self._in_flight = 0
        self._idle = asyncio.Event()
        self._idle.set()
        self._published: list[Published] = []
        self._subscribed: list[Subscribed] = []
        self._handler_errors: list[Exception] = []
        self._responses: dict[str | None, int] = {}

    @property
    def published(self) -> tuple[Published, ...]:
        """Every publish on this broker, in order, replies included."""
        return tuple(self._published)

    @property
    def subscribed(self) -> tuple[Subscribed, ...]:
        """Every `subscribe` made on this broker, in order, including ones since ended."""
        return tuple(self._subscribed)

    @property
    def handler_errors(self) -> tuple[Exception, ...]:
        """What subscription callbacks raised, in order; each also went to its `error_cb`."""
        return tuple(self._handler_errors)

    async def connect(
        self,
        url: str | None = None,
        *,
        name: str | None = None,
        error_cb: ConnectionCallback | None = None,
        disconnected_cb: ConnectionCallback | None = None,
        closed_cb: ConnectionCallback | None = None,
        **options: Any,
    ) -> _Connection:
        """A new connection on this broker.

        Takes what `nats.connect` takes, so `ServiceTestHarness(broker=)` can dial it as it dials
        a server. The URL is not read, and of the dial options only `user`, whose response grant
        `allow_responses` sets; the three callbacks are called as a client calls them.
        """
        return _Connection(
            self,
            name=name,
            error_cb=error_cb,
            disconnected_cb=disconnected_cb,
            closed_cb=closed_cb,
            response_max=self._responses.get(options.get("user")),
        )

    def allow_responses(self, user: str, response_max: int) -> None:
        """Grant connections dialled as `user` `response_max` replies to each request delivered to
        them, as a broker user's `allow_responses.max` does; `-1` for no limit. A publish past the
        count is not delivered, and is reported to the connection's `error_cb` as nats-py reports
        the broker's refusal. Only the count is modelled, not the grant's expiry. A connection
        dialled before the grant does not take it."""
        if isinstance(response_max, bool) or not (response_max == -1 or response_max >= 1):
            raise ValueError("response_max must be -1 (no limit) or a positive count of replies")
        self._responses[user] = response_max

    def _has_subscriber_for(self, subject: str) -> bool:
        return any(_subject_matches(s.subject, subject) for s in self._subscriptions)

    def _route(
        self,
        sender: _Connection,
        subject: str,
        payload: bytes,
        reply: str | None,
        headers: dict[str, str] | None,
    ) -> None:
        frozen = MappingProxyType(dict(headers)) if headers else None
        self._published.append(Published(subject, payload, frozen, reply))
        if not self._deliver(subject, payload, reply, headers) and reply:
            self._no_responders(sender, reply)

    def _no_responders(self, sender: _Connection, reply: str) -> None:
        """The 503 the server sends for a message nobody held, as it sends it: to the publishing
        connection only, on one of its own subscriptions outside a queue group that holds `reply`
        (the server picks one; this broker picks the last), and to nothing when it has none. It is the broker's, not a publish, so it is not
        recorded."""
        own = [
            sub
            for sub in self._subscriptions
            if sub.connection is sender
            and sub.queue is None
            and _subject_matches(sub.subject, reply)
        ]
        if own:
            self._hand(own[-1], reply, b"", None, {"Status": "503"})

    def _deliver(
        self, subject: str, payload: bytes, reply: str | None, headers: dict[str, str] | None
    ) -> bool:
        """Hand a message to every subscriber outside a queue group and to one member of each
        group, rotating; whether any subscription held `subject`."""
        groups: dict[str, list[_Subscription]] = {}
        held = False
        for sub in list(self._subscriptions):
            if not _subject_matches(sub.subject, subject):
                continue
            held = True
            if sub.queue is None:
                self._hand(sub, subject, payload, reply, headers)
            else:
                groups.setdefault(sub.queue, []).append(sub)
        for queue, members in groups.items():
            turn = next(self._rotation.setdefault(queue, itertools.count()))
            self._hand(members[turn % len(members)], subject, payload, reply, headers)
        return held

    def _hand(
        self,
        sub: _Subscription,
        subject: str,
        payload: bytes,
        reply: str | None,
        headers: dict[str, str] | None,
    ) -> None:
        self._idle.clear()
        if reply is not None:
            sub.connection._granted_reply(reply)
        sub.deliver(
            _Message(sub.connection, subject, payload, dict(headers) if headers else None, reply)
        )

    def _delivered(self) -> None:
        self._in_flight -= 1
        if self._in_flight == 0:
            self._idle.set()

    async def settle(self, timeout: float = 5.0) -> None:
        """Return once every delivery handed out so far, and any it led to, has been run.

        Bounded, so a callback that never returns fails here by name rather than hanging the test.
        It waits for the callbacks only: a task a callback spawned is not waited for.
        """
        try:
            while True:
                await asyncio.wait_for(self._idle.wait(), timeout=timeout)
                # One more turn, so a delivery a just-finished callback published is counted.
                await asyncio.sleep(0)
                if self._idle.is_set():
                    return
        except TimeoutError:
            raise AssertionError(
                f"the broker did not settle within {timeout}s: {self._in_flight} deliveries "
                "still running or queued"
            ) from None
