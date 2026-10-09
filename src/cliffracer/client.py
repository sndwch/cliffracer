"""The base class extended by generated clients.

A generated client is a subclass with one stub per RPC method and four class
attributes recording what it was generated FROM. Everything that talks to the
broker is here, so the generated file stays a description of the service rather
than a copy of the transport.

WHAT `verify` IS FOR. A client is code generated from a snapshot. The service
moves; the client does not. Without a check, the first symptom of drift is a
field that will not deserialise, or -- worse -- a call that succeeds and means
something else. `verify` asks the running service to describe itself and
compares `signature_hash` per method, so the error names the METHOD that
changed. It runs once per connection, lazily, on the first call.

A METHOD THE SERVICE ADDED IS NOT DRIFT. Only the methods this client carries
are compared: a service that grew a handler is still able to serve every call
this client knows how to make. The reverse -- a method gone, or its signature
changed -- is what makes the client wrong.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import os
import re
import time
import types
import uuid
from collections import deque
from collections.abc import AsyncGenerator, Callable
from functools import lru_cache
from typing import Annotated, Any, Self, Union, get_args, get_origin

import nats
from loguru import logger
from nats.aio.msg import Msg
from nats.errors import Error as NatsError
from nats.errors import NoRespondersError as NoResponders
from nats.errors import NoServersError
from nats.errors import TimeoutError as NatsTimeout
from pydantic import BaseModel, TypeAdapter
from pydantic import ValidationError as PydanticValidationError

from cliffracer.core import dial
from cliffracer.core.connection import redact_nats_url
from cliffracer.core.correlation import CorrelationContext
from cliffracer.core.deadline import (
    TIMEOUT_HEADER,
    header_value,
    outbound_timeout,
    refuse_a_duration,
)
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import (
    ClientError,
    ClientOutOfDate,
    ClientOutOfDateError,
    RpcBusyError,
    RpcClientError,
    RpcConnectionError,
    RpcDeadlineExceededError,
    RPCError,
    RpcError,
    RpcNoResponders,
    RpcNoRespondersError,
    RpcRefused,
    RpcRefusedError,
    RpcServerError,
    RpcStreamGapError,
    RpcTimeout,
    RPCTimeoutError,
    RpcTimeoutError,
    RpcUnknownMethod,
    RpcUnknownMethodError,
    RpcValidationError,
    raise_for_error_envelope,
)
from cliffracer.core.nats_errors import rpc_error_for
from cliffracer.core.rpc_calls import read_reply, require_success
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.stream_reader import open_stream, read_stream
from cliffracer.core.subjects import validate_inbox_prefix
from cliffracer.core.validation import (
    CONTENT_TYPE_JSON,
    ExtraForms,
    _reads_faithfully,
    choose_wire_form,
    faithful_to,
    nested_form,
    read_python_then_json,
    refuse_a_lost_value,
    with_subclass_fields_at_open_bases,
)
from cliffracer.introspect import Description

# One adapter per annotation, for the life of the process.
#
# Building a `TypeAdapter` compiles a pydantic-core schema and a serializer.
# Doing it inside a call is the expensive half and the result was thrown away
# immediately: on `list[Order]` with a nested `list[Item]`, 48.4us per argument
# against 2.0us for an adapter already built. A three-argument method paid that
# three times per request, which is the same order as a local NATS round trip.
#
# The server has always done this the other way -- `typed_rpc.py` precomputes an
# adapter per method at discovery and reuses it at dispatch -- so this brings
# the client into line rather than inventing a scheme.
#
# Keyed on the annotation rather than on (method, argument): the annotation is
# all `_encode` receives, and keying on it means two methods taking the same
# type share one adapter instead of holding two identical ones. Bounded because
# a client's annotations are a small fixed set; the bound exists so a caller
# generating types at runtime cannot grow this without limit.
ADAPTER_CACHE_SIZE = 512


@lru_cache(maxsize=ADAPTER_CACHE_SIZE)
def _adapter_for(annotation: Any) -> TypeAdapter:
    """The `TypeAdapter` for one annotation, built once."""
    return TypeAdapter(annotation)


def _adapter(annotation: Any) -> TypeAdapter:
    """`_adapter_for`, except for an annotation that cannot be a cache key.

    `Annotated[int, ["note"]]` is a legal annotation that `TypeAdapter` accepts,
    and it is unhashable, so looking it up would raise `TypeError` where the
    uncached code encoded it fine. Hashability is tested directly rather than
    catching `TypeError` from the lookup, so that a `TypeError` raised by
    building the adapter itself still reaches the caller unchanged.
    """
    try:
        hash(annotation)
    except TypeError:
        return TypeAdapter(annotation)
    return _adapter_for(annotation)


__all__ = [
    "ServiceClient",
    "RpcError",
    "RpcClientError",
    "RpcServerError",
    "RpcStreamGapError",
    "RpcTimeoutError",
    "RpcDeadlineExceededError",
    "RpcBusyError",
    "RpcConnectionError",
    "RpcNoRespondersError",
    "RpcValidationError",
    "RpcUnknownMethodError",
    "RpcRefusedError",
    "ClientOutOfDateError",
    "ClientError",
    "RPCError",
    "RPCTimeoutError",
    "RpcTimeout",
    "RpcNoResponders",
    "RpcUnknownMethod",
    "RpcRefused",
    "ClientOutOfDate",
]


def _annotation_name(annotation: Any) -> str:
    """A readable name for an annotation, for an error a caller has to act on.

    Built from the origin and arguments rather than read off one attribute.
    A parameterised generic DOES have a `__name__` -- `list[Order].__name__` is
    `"list"` -- so reading that first answers "list is not a valid list" for a
    bad `list[Order]`, naming the container and losing the part that failed.
    And `str()` on a union keeps the module path, which is what buries the name
    the caller recognises.
    """
    origin = get_origin(annotation)
    if origin is None:
        if annotation is type(None):
            return "None"
        return getattr(annotation, "__name__", None) or str(annotation).replace("typing.", "")
    args = get_args(annotation)
    if origin in (types.UnionType, Union):
        return " | ".join(_annotation_name(a) for a in args)
    name = getattr(origin, "__name__", None) or str(origin).replace("typing.", "")
    if not args:
        return name
    return f"{name}[{', '.join(_annotation_name(a) for a in args)}]"


# What nats-py reports of a refusal by the broker: `nats: permissions violation for publish to "x"`.
_VIOLATION = re.compile(
    r'permissions violation for (?P<kind>publish|subscription) to "(?P<subject>[^"]*)"', re.I
)


def _equal(read: Any, value: Any) -> bool:
    """`read == value`, with a comparison that raises counted as not equal."""
    try:
        return bool(read == value)
    except Exception:
        return False


def _refuse_a_lost_value_through(
    annotation: Any, form: Any, value: Any, at: tuple[int | str, ...] = ()
) -> None:
    """`refuse_a_lost_value` for `value` when `annotation` is its model class, and for each model it
    holds in a list, a tuple (of any length, or one type per position), a dict, an optional or an
    `Annotated`, each read from its own place in `form` and named there by its index or key. A
    model in a container is refused where the same model passed bare is. In a union, a model is
    checked as the one member class it is an instance of; one that is an instance of two members
    (a subclass under `Base | Sub`) is left alone, since which of them the service reads it as is
    the union's to decide.
    """
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if isinstance(value, BaseModel):
            refuse_a_lost_value(annotation, form, value, at)
        return
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        _refuse_a_lost_value_through(args[0], form, value, at)
        return
    if origin in (Union, types.UnionType):
        if value is None:
            return
        members = [arg for arg in args if arg is not type(None)]
        if len(members) == 1:
            _refuse_a_lost_value_through(members[0], form, value, at)
            return
        held_as = [m for m in members if _is_model_class_holding(m, value)]
        if len(held_as) == 1:
            _refuse_a_lost_value_through(held_as[0], form, value, at)
        return
    if origin is dict and len(args) == 2 and isinstance(value, dict) and isinstance(form, dict):
        for key, item in value.items():
            # The JSON form writes a key that is not a string (an `int`) as one.
            written = key if key in form else str(key)
            if written in form:
                _refuse_a_lost_value_through(args[1], form[written], item, (*at, key))
        return
    if not isinstance(value, list | tuple) or not isinstance(form, list | tuple):
        return
    if len(form) != len(value):
        return
    if (origin is list and len(args) == 1) or (
        origin is tuple and len(args) == 2 and args[1] is Ellipsis
    ):
        for index, (item, written) in enumerate(zip(value, form, strict=True)):
            _refuse_a_lost_value_through(args[0], written, item, (*at, index))
    elif origin is tuple and args and len(args) == len(value):
        for index, (arg, item, written) in enumerate(zip(args, value, form, strict=True)):
            _refuse_a_lost_value_through(arg, written, item, (*at, index))


def _is_model_class_holding(member: Any, value: Any) -> bool:
    """Whether `member`, or the type an `Annotated` member wraps, is a model class `value` is an
    instance of."""
    if get_origin(member) is Annotated:
        member = get_args(member)[0]
    return isinstance(member, type) and issubclass(member, BaseModel) and isinstance(value, member)


def _faithful_through(annotation: Any, value: Any) -> Callable[[Any], bool] | None:
    """`faithful_to` for each model `value` holds in a list, a tuple (of any length, or one type per
    position), a dict, a union or an `Annotated`: whether what `annotation` reads holds, in each
    model's place, what that model makes of the caller's field values, and everything else equal.
    A union is read faithfully when any of its members reads it so. None when it holds no model that
    can be read so, which leaves equality as the only test."""
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        return faithful_to(annotation, value) if isinstance(value, BaseModel) else None
    origin, args = get_origin(annotation), get_args(annotation)
    if origin is Annotated:
        return _faithful_through(args[0], value)
    if origin in (Union, types.UnionType):
        if value is None:
            return None
        members = [_faithful_through(arg, value) for arg in args if arg is not type(None)]
        member_tests = [test for test in members if test is not None]
        if not member_tests:
            return None
        if len(member_tests) == 1:
            return member_tests[0]

        def faithful_member(read: Any) -> bool:
            return any(test(read) for test in member_tests)

        return faithful_member
    if origin is dict and len(args) == 2 and isinstance(value, dict):
        tests = {key: _faithful_through(args[1], item) for key, item in value.items()}
        if all(test is None for test in tests.values()):
            return None

        def faithful_dict(read: Any) -> bool:
            if not isinstance(read, dict) or not _equal(list(read), list(value)):
                return False
            return all(
                test(read[key]) if test is not None else _equal(read[key], value[key])
                for key, test in tests.items()
            )

        return faithful_dict
    homogeneous = (origin is list and len(args) == 1) or (
        origin is tuple and len(args) == 2 and args[1] is Ellipsis
    )
    if homogeneous and isinstance(value, list | tuple):
        items = [(item, _faithful_through(args[0], item)) for item in value]
        if all(test is None for _, test in items):
            return None

        def faithful_items(read: Any) -> bool:
            if not isinstance(read, list | tuple) or len(read) != len(items):
                return False
            return all(
                test(got) if test is not None else _equal(got, item)
                for got, (item, test) in zip(read, items, strict=True)
            )

        return faithful_items
    fixed = origin is tuple and not (len(args) == 2 and args[1] is Ellipsis) and bool(args)
    if fixed and isinstance(value, list | tuple) and len(value) == len(args):
        placed = [
            (item, _faithful_through(arg, item)) for arg, item in zip(args, value, strict=True)
        ]
        if all(test is None for _, test in placed):
            return None

        def faithful_positions(read: Any) -> bool:
            if not isinstance(read, list | tuple) or len(read) != len(placed):
                return False
            return all(
                test(got) if test is not None else _equal(got, item)
                for got, (item, test) in zip(read, placed, strict=True)
            )

        return faithful_positions
    return None


def _reads_back_faithfully(
    read: Callable[[Any], Any], annotation: Any, wire: Any, value: Any
) -> bool:
    """Whether `read`, the annotation's validation, reads `wire` as `value`: equal; or, for a model
    argument and a model annotation, equal in each field the annotation declares (`value` may be a
    subclass), or as the annotation's validators make of the caller's values (`_reads_faithfully`);
    or, for models in a list, tuple, dict or optional, each so in its place (`_faithful_through`)."""
    if (
        isinstance(value, BaseModel)
        and isinstance(annotation, type)
        and issubclass(annotation, BaseModel)
    ):
        return _reads_faithfully(annotation, wire, value)
    try:
        back = read(wire)
    except Exception:
        return False
    if _equal(back, value):
        return True
    faithful = _faithful_through(annotation, value)
    return faithful is not None and faithful(back)


class ServiceClient:
    """Transport, verification and error mapping for a generated client.

    SERVICE / VERSION / DESCRIPTION_HASH / SIGNATURES are written by the
    generator. `DESCRIPTION_HASH` records which description the file was
    generated from -- provenance for a human and for the generator's own
    regeneration check -- while `SIGNATURES` is what `verify` compares, because
    a per-method hash can name the method that moved and a whole-description
    hash cannot.

    `NAMESPACE` is written when the client was generated with `--namespace`,
    and is the namespace a client constructed without `namespace=` calls in.
    `namespace=""` calls with no namespace, since a subject is scoped only by a
    non-empty one.

    Because `{service}.describe` is subscribed in the RPC queue group, `verify`
    samples a single replica from the running fleet. During a rolling deploy
    with mixed versions, `_call` automatically re-verifies if an
    `RpcValidationError` occurs, detecting schema drift and raising `ClientOutOfDate`.
    """

    SERVICE: str = ""
    NAMESPACE: str | None = None
    VERSION: str = ""
    DESCRIPTION_HASH: str = ""
    SIGNATURES: dict[str, str] = {}

    def __init__(
        self,
        nc: nats.NATS | None = None,
        *,
        nats_url: str | None = None,
        service: str | None = None,
        namespace: str | None = None,
        subject_prefix: str | None = None,
        inbox_prefix: str | None = None,
        timeout: float = 30.0,
        connect_timeout: float | None = 30.0,
        headers: dict[str, str] | None = None,
        verify: bool = True,
    ) -> None:
        if inbox_prefix is not None:
            validate_inbox_prefix(inbox_prefix)
            if nc is not None:
                raise ValueError("set inbox_prefix when opening the borrowed NATS connection")
        self._inbox_prefix = inbox_prefix
        self._nc = nc
        self._nats_url = nats_url
        self._owns_nc = nc is None
        self._connect_lock = asyncio.Lock()
        self._verify_lock = asyncio.Lock()
        #: The re-verification currently in flight, shared by the cohort that
        #: hit the same drift. Cleared when it settles, so the NEXT
        #: `RpcValidationError` starts a fresh one -- this is a single-flight,
        #: not a memo, and the difference is the whole point on this path.
        self._reverify: asyncio.Task[None] | None = None
        self.service = service or self.SERVICE
        self.namespace = self.NAMESPACE if namespace is None else namespace
        self.subject_prefix = subject_prefix
        self._validate_subject_parts()
        refuse_a_duration("timeout", timeout)
        if connect_timeout is not None:
            refuse_a_duration("connect_timeout", connect_timeout)
        self.timeout = timeout
        self.connect_timeout = connect_timeout
        self.headers = dict(headers or {})
        self._verify = verify
        self._verified = False
        self._closed = False
        #: The permissions violations the broker reported on an owned connection, as (when it was
        #: reported, the kind, the subject it named, the text). The broker reports one through the
        #: connection's error callback while the request that caused it waits for a reply that
        #: cannot come, so a timeout reads the ones reported during its own wait.
        self._violations: deque[tuple[float, str, str, str]] = deque(maxlen=32)

    def _note_a_violation(self, error: BaseException) -> None:
        """Keep a permissions violation the broker reported; every other error is not kept."""
        text = str(error)
        found = _VIOLATION.search(text)
        if found:
            self._violations.append(
                (time.monotonic(), found["kind"], found["subject"], text.removeprefix("nats: "))
            )

    def _violation_during(self, began: float, subject: str) -> str:
        """What to add to a timeout of the request to `subject` that began at `began`: every
        violation of its wait, oldest first, or nothing when there is none.

        Only a violation reported since the request began counts. One for a publish is that
        request's when it names the request's subject, since a publish is refused per subject. One
        for a subscription is every waiting request's: the replies to all of a client's requests
        come back on one inbox subscription. The text names the subject the broker gave, so a
        violation that is not this request's reads as one.
        """
        reported = [
            (kind, text)
            for when, kind, named, text in self._violations
            if when >= began and (kind != "publish" or named.lower() == subject.lower())
        ]
        if not reported:
            return ""
        hint = (
            " A client role the broker confines to an inbox prefix needs inbox_prefix= naming it."
            if any(kind == "subscription" for kind, _ in reported)
            else ""
        )
        said = "; then ".join(text for _, text in reported)
        return f"; the broker reported while it waited: {said}.{hint}"

    def _validate_subject_parts(self) -> None:
        """Refuse a service, namespace or subject prefix that cannot be part of a subject.

        The same rules `ServiceConfig` applies to them, which is where they come from: a value it
        would refuse builds a subject nothing serves, so the call waited out `timeout`, and one with
        white space is a malformed frame on a connection that may be shared with the application.
        An empty namespace or prefix means none, and an empty service is refused when a call is
        made, since a generated client names it and `ServiceClient` takes it as an argument.
        """
        # Each value is checked with the prefix pinned: `ServiceConfig` reads its own prefix from
        # the environment, and a bad one there would otherwise be reported against whichever
        # field was being checked. The environment's prefix is the client's only when none is given.
        from_environment = (
            os.environ.get("CLIFFRACER_SUBJECT_PREFIX") if self.subject_prefix is None else None
        )
        problems = []
        for label, value, kwargs in (
            ("service", self.service, {"name": self.service, "subject_prefix": None}),
            (
                "namespace",
                self.namespace,
                {"name": "client", "namespace": self.namespace, "subject_prefix": None},
            ),
            (
                "subject_prefix",
                self.subject_prefix,
                {"name": "client", "subject_prefix": self.subject_prefix},
            ),
            (
                "subject_prefix (from CLIFFRACER_SUBJECT_PREFIX)",
                from_environment,
                {"name": "client", "subject_prefix": from_environment},
            ),
        ):
            if not value:
                continue
            try:
                ServiceConfig.model_validate(kwargs)
            except PydanticValidationError as exc:
                problems.append(f"{label}={value!r}: {exc.errors()[0]['msg']}")
        if problems:
            raise ValueError("the client cannot address a service with " + "; ".join(problems))

    async def _connection(self) -> nats.NATS:
        """The connection, opened on first use if the caller supplied none.

        RE-VERIFICATION AFTER A RECONNECT HAPPENS ONLY FOR A CONNECTION THIS
        CLIENT OWNS. nats-py takes `reconnected_cb` at `connect()` time and
        keeps one callback, so a connection handed in cannot be given ours
        without overwriting whatever its owner registered. An earlier version
        of this method looked for an `add_reconnect_callback` method that
        `nats.NATS` does not have, which made the branch inert and the
        limitation invisible; saying it here is better than a hook that never
        fires. A reconnect can mean the service was restarted, and a restarted
        service can be a different build, which is exactly when the drift check
        earns its keep -- so a long-lived borrowed connection is a reason to
        call `verify()` yourself.
        """
        if self._closed:
            raise RpcConnectionError(
                f"the client for {self.service!r} was closed; construct a new one"
            )
        # No connection yet, or one nats-py closed for good (an authentication change or a terminal
        # error from the server, ADR-0008): the object stays assigned and refuses every request. A
        # connection this client opened is dialled again; one it was handed is its owner's to
        # replace. A client that was closed never gets here, `close()` being an intent to stop.
        # `is True`: a double standing in for a connection answers every attribute with a truthy mock.
        nc = self._nc
        if nc is None or (self._owns_nc and nc.is_closed is True):
            async with self._connect_lock:
                nc = self._nc
                if nc is None or (self._owns_nc and nc.is_closed is True):
                    replacing = nc is not None
                    dialled = await self._dial()
                    if self._was_closed():
                        # `close()` ran while this dial was in flight, when there was no connection
                        # to release. Nothing else will ever close the one just made, and it would
                        # reconnect for as long as the process lives.
                        await self._release(dialled)
                        raise RpcConnectionError(
                            f"the client for {self.service!r} was closed; construct a new one"
                        )
                    nc = self._nc = dialled
                    if replacing:
                        # A new connection can be a restarted service, and a different build.
                        self._verified = False
        return nc

    async def _dial(self) -> nats.NATS:
        """Open the connection, bounded by `connect_timeout` and retrying forever.

        The two settings look contradictory and are not. ADR-0008 wants
        `max_reconnect_attempts=-1` so a broker restart does not kill a running
        caller; ADR-0012 observes that on the FIRST dial that same setting is
        what makes nats-py wait forever, and bounds it with `connect_timeout`.
        Leaving both unset -- which is what this method used to do -- takes
        nats-py's defaults instead: 60 attempts two seconds apart, so an
        unreachable broker failed after about two minutes no matter what the
        caller configured, and then stopped reconnecting for good.

        `None` disables the bound, the same spelling and meaning
        `ServiceConfig.connect_timeout` documents.
        """
        url = self._nats_url or "nats://localhost:4222"
        inbox_options = {"inbox_prefix": self._inbox_prefix} if self._inbox_prefix else {}
        last_error: list[BaseException] = []

        async def remember(error: Exception) -> None:
            last_error[:] = [error]
            self._note_a_violation(error)

        try:
            return await dial.connect(
                url,
                timeout=self.connect_timeout,
                reconnected_cb=self._on_reconnect,
                error_cb=remember,
                max_reconnect_attempts=-1,
                **inbox_options,
            )
        except TimeoutError as exc:
            # nats-py retries an authorization failure for as long as it is allowed to, so what
            # the caller waited out was an answer that said no. The last error it reported is
            # the reason, and it is the only place the reason appears.
            why = (
                f"; the last error from the broker was {type(last_error[0]).__name__}: "
                f"{last_error[0]}"
                if last_error
                else ""
            )
            raise RpcConnectionError(
                f"{redact_nats_url(url)} did not answer within "
                f"connect_timeout={self.connect_timeout}s{why}"
            ) from exc
        except NoServersError as exc:
            raise RpcConnectionError(f"no broker reachable at {redact_nats_url(url)}") from exc

    async def _on_reconnect(self) -> None:
        self._verified = False

    def _headers_for_send(self) -> dict[str, str]:
        """The caller's headers plus the correlation id this request belongs to.

        Resolved in the order `publish_event` uses: an id the caller set
        explicitly, then the AMBIENT one, then a new one.

        The ambient step is what keeps a trace whole. A service handling an
        inbound request and calling a peer through a generated client is one
        hop of that request, not the start of a new one -- and the service's own
        outbound path has always read `CorrelationContext`, so without this the
        two supported ways of calling the same peer disagreed about whether the
        trace survived.

        A new id per request is still the answer when there is no ambient one,
        which is what keeps unrelated calls from sharing an identifier.
        """
        # Read the way the service reads it: any case, and any of the names it accepts. The id the
        # caller set under `X-Correlation-Id`, the usual HTTP spelling, was otherwise ignored and
        # replaced by a new one the service then preferred, which broke the trace without a word.
        cid = (
            CorrelationContext.extract_from_headers(self.headers)
            or CorrelationContext.ambient_for_send()
            or uuid.uuid4().hex
        )
        # The two spellings written below replace the caller's own, whatever their case: two
        # headers that differ only in case are one header to the service and the first would win.
        kept = {
            name: value
            for name, value in self.headers.items()
            if name.lower() not in {"x-correlation-id", "correlation_id"}
        }
        return {**kept, "X-Correlation-ID": cid, "correlation_id": cid}

    def _subject(self, tail: str) -> str:
        """The subject the service answers on: `<prefix>.<namespace>.<service>.<tail>`.

        The environment prefix goes outside the namespace, matching the order
        the service subscribes in. This client holds no `ServiceConfig` -- it is
        generated code a caller constructs -- so the prefix comes from the same
        environment variable `ServiceConfig.subject_prefix` defaults from.
        An explicit `subject_prefix` pins the address independently of the
        environment; the empty string selects an unprefixed address.
        """
        if not self.service:
            raise RpcClientError(
                "this client names no service: pass service= or set SERVICE on the class"
            )
        return HandlerDiscovery.scoped_subject(
            f"{self.service}.{tail}",
            namespace=self.namespace,
            subject_prefix=(
                os.environ.get("CLIFFRACER_SUBJECT_PREFIX") or None
                if self.subject_prefix is None
                else self.subject_prefix or None
            ),
        )

    async def _request(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> Msg:
        """Dispatch a NATS request and translate network errors into ClientError exceptions.

        The request waits `timeout`, or less when it is made inside a handler whose own request
        has less left, and carries what it waits in `Cliffracer-Timeout-Ms`. A request made
        when that has run out is not sent.
        """
        timeout = outbound_timeout(self.timeout, subject)
        waits = self.timeout if timeout == self.timeout else round(timeout, 3)
        sent = {
            name: value
            for name, value in (headers or {}).items()
            if name.lower() != TIMEOUT_HEADER.lower()
        }
        sent[TIMEOUT_HEADER] = header_value(timeout)
        nc = await self._connection()
        began = time.monotonic()
        try:
            return await nc.request(subject, payload, timeout=timeout, headers=sent)
        except NatsTimeout as exc:
            raise RpcTimeoutError(
                f"{subject} did not answer within {waits}s{self._violation_during(began, subject)}"
            ) from exc
        except NoResponders as exc:
            raise RpcNoRespondersError(
                f"nothing is subscribed to {subject}; is the service running?"
            ) from exc
        except NatsError as exc:
            # Whatever else nats-py raises on the request path is a failure of the connection or
            # of the request it was asked to carry; a caller's `except RpcError` holds it.
            raise rpc_error_for(exc, subject, awaiting_reply=True) from exc

    @staticmethod
    def _decode_reply(reply: Msg, subject: str) -> dict[str, Any]:
        """The reply as an object, or an `RpcServerError` saying what came back: `read_reply`,
        the reading `call_rpc` and `cliffracer.calls` apply too."""
        return read_reply(reply, subject, "json")

    @staticmethod
    def _preview(payload: object, limit: int = 120) -> str:
        """A short, printable look at a payload, for a message a human reads.

        Typed `object` because the failures this appears in are exactly the ones
        where the reply is not the shape its annotation claims.
        """
        if not isinstance(payload, bytes | bytearray):
            return repr(payload)[:limit]
        text = bytes(payload[:limit]).decode("utf-8", errors="replace")
        return f"{text!r}{'...' if len(payload) > limit else ''}"

    def _raise_for_error(self, data: dict[str, Any], subject: str) -> None:
        """Turn an error envelope into the corresponding exception, or return.

        The one reading of the envelope is `raise_for_error_envelope`, shared with the service's
        own `call_rpc`.
        """
        raise_for_error_envelope(data, subject)

    async def _verify_once(self) -> None:
        """Verify lazily on first use, once per connection however many calls race.

        `_connection()` has always serialised connecting; nothing serialised
        verifying. `verify()` sets `_verified` only after a full round trip, so
        every call that started before the first describe returned saw `False`
        and sent its own: 50 concurrent first calls sent 50 describes, against a
        docstring promising one per connection.

        That is not only waste. Describe is served from the RPC queue group, so
        N describes sample N arbitrary replicas, and during a rolling deploy
        concurrent calls could verify against different builds and disagree.

        The flag is re-read INSIDE the lock, so the calls that queued behind the
        winner return without asking again. A failed verification is not
        recorded -- `_verified` stays false and the next call retries -- because
        caching a failure would make a transient one permanent, and because a
        client that is genuinely out of date must keep saying so.

        `verify()` itself is untouched and still asks every time it is called.
        It is the public "ask the service to describe itself", and `_call`
        re-invokes it on `RpcValidationError` precisely to catch a replica that
        moved; that must not be answered from a flag.
        """
        if self._verified:
            return
        async with self._verify_lock:
            # Annotated so the type checker does not narrow this from the check
            # above and call the branch unreachable. Another task can set the
            # flag while this one waits for the lock -- that is the whole reason
            # the flag is read a second time here.
            verified: bool = self._verified
            if verified:
                return
            await self.verify()

    async def _reverify_together(self) -> None:
        """Re-verify after a drift reply, once for everyone who saw it.

        `_call` re-invokes `verify()` when a reply comes back
        `RpcValidationError`, because in a rolling deploy the request may have
        hit a newer replica. That is right, and it is why this cannot be
        answered from `_verified`: the flag is already True by then, and reading
        it would disable the drift detection this path exists for.

        But nothing shared the answer either, so a batch of in-flight calls that
        all failed validation together sent one describe EACH -- measured at 50
        for 50 concurrent calls. Describe is served from the RPC queue group, so
        those N requests sample N arbitrary replicas: the same fan-out, and the
        same disagreement, on the path the rolling deploy actually causes.

        A single-flight rather than a lock. A lock would serialise the cohort
        and still ask N times; what is wanted is for the calls arriving while a
        re-verification is in flight to await THAT one. A SETTLED task is not
        reused, so a later drift asks again -- caching the answer would be the
        memo this path must not become, and would be worse than the fan-out.

        Cancellation: the task is created detached and awaited, so a caller
        going away does not cancel the re-verification for the cohort still
        waiting on it. An exception -- `ClientOutOfDate` is the expected one --
        reaches every awaiter, which is what each would have got alone.
        """
        task = self._reverify
        if task is None or task.done():
            # `done()` is what makes the next drift ask again: a settled task is
            # a spent answer. A done-callback clearing the slot was here first
            # and was dead -- removing it reddened nothing, because this check
            # had already covered every case it claimed to.
            task = asyncio.create_task(self.verify())
            self._reverify = task

            # Retrieve the result if nobody else does. `shield` normally does
            # this for a cancelled awaiter, but only while its callback is still
            # registered: cancelling the outer BEFORE the inner settles removes
            # it, so a cohort that all goes away leaves the exception unclaimed
            # and asyncio logs "Task exception was never retrieved" at ERROR --
            # during a rolling deploy, which is when someone is reading the log.
            #
            # NOT the done-callback that was here before and was dead. That one
            # cleared the slot, which `done()` already covered. This one has an
            # effect nothing else has, and `test_a_fully_cancelled_cohort_leaves_
            # no_unretrieved_exception` reds without it.
            def _retrieve(finished: asyncio.Task[None]) -> None:
                if not finished.cancelled():
                    finished.exception()

            task.add_done_callback(_retrieve)
        await asyncio.shield(task)

    async def verify(self) -> None:
        """Compare this client's per-method hashes with the running service.

        The client must declare non-empty `SIGNATURES` mapping method names to
        expected signature hashes; calling `verify()` without signatures raises
        `ClientError`. To bypass verification for clients without declared
        signatures, instantiate the client with `verify=False`.

        In a multi-replica or rolling deployment, this request samples one replica
        from the RPC queue group. If an individual RPC call subsequently fails with
        an `RpcValidationError`, `_call` re-runs `verify()` to detect whether
        another replica in the fleet has updated its signatures.
        """
        if not self.SIGNATURES:
            raise ClientError(
                f"client for {self.service!r} declares no signatures; "
                f"set SIGNATURES or pass verify=False"
            )
        # Send authentication headers with describe requests so services
        # requiring authorization accept the verification request.
        subject = self._subject("describe")
        reply = await self._request(subject, b"", self._headers_for_send())
        data = self._decode_reply(reply, subject)
        self._raise_for_error(data, subject)
        try:
            live = Description.from_dict(data)
        except (KeyError, TypeError, ValueError) as exc:
            # `{service}.describe` is a plain subject with no ownership, so a
            # namespace slip or a stale deployment puts a foreign responder on
            # it. `from_dict` indexed the payload and raised first, which made
            # the service-name check below unreachable for exactly the case it
            # exists to catch. Reported as the caller's, because the fix is
            # almost always `service=` or `namespace=`.
            raise ClientError(
                f"{subject} was answered by something that is not a cliffracer "
                f"service description ({exc!r}); check service= and namespace=. "
                f"The reply was: {self._preview(reply.data)}"
            ) from exc
        if live.service != self.service:
            # The subject resolved to something else: a `service=` or
            # `namespace=` slip. The hashes could still line up by coincidence
            # of shape, so this is checked by name rather than left to them.
            raise ClientError(
                f"{subject} is served by {live.service!r}, not {self.service!r}; "
                f"check service= and namespace="
            )
        changed, missing = [], []
        for name, sig in self.SIGNATURES.items():
            m = live.method(name)
            if m is None:
                missing.append(name)
            elif m.signature_hash != sig:
                changed.append(name)
        if changed or missing:
            # A client that is out of date keeps saying so: the flag a success set is cleared, or
            # the next call would skip the check and the drift would go unreported while the
            # service still accepted the call.
            self._verified = False
            raise ClientOutOfDate(self.service, changed, missing)
        self._verified = True

    def _encode(self, value: Any, annotation: Any) -> Any:
        """JSON-ready value for one argument, through its DECLARED annotation.

        The generated stubs pass the annotation, so a `list[Order]` argument is
        dumped as a list of Order dicts. `TypeAdapter(type(value))` would see
        `list` and lose the item type; that is why the annotation travels from
        the stub rather than being inferred here.

        CHECKED BEFORE IT IS SENT. `dump_python` serialises without validating,
        so a value the annotation does not accept went out with nothing but a
        pydantic serializer warning, and the caller learned of it a round trip
        later as a `RpcValidationError` attributed to the service -- for a
        mistake that was visible in the argument they passed.

        The objection to checking here is that the service is the authority,
        and a client on an older type could refuse a call a newer service would
        take. Two things answer it. An arbitrary union is not an expressible
        parameter type, so `int` cannot widen to `int | str`; and the widenings
        that ARE expressible change the method's signature hash, which is what
        `verify()` compares, so that drift already surfaces as
        `ClientOutOfDate` rather than as a wrong local refusal.

        THE VALIDATED RESULT IS DISCARDED. This is a check, not a coercion
        step: `dump_python` still receives the caller's own object, so every
        value that travelled before this existed travels identically. Were the
        validated value dumped instead, a coercible argument would quietly
        change on the wire.

        THE FORM IS THE ONE THE SERVICE READS AS THE ARGUMENT. The service
        validates the model under its own config, so a model that is only
        readable by its alias (an `alias` without `populate_by_name`, an
        `alias_generator` without it, such a model nested or in a list) is
        refused when sent by field name, and one whose aliases are each other's
        field names is accepted by name and read with its values swapped.
        Sending by alias always would break the models that are accepted by name
        and refused by alias (`validate_by_alias=False`, a `serialization_alias`
        that differs from the `validation_alias`). So the plain dump is tried
        first, which the model's own `serialize_by_alias` decides, then the
        alias form, then the dump by field name, then a form written one model
        at a time for a tree whose levels need different forms (`nested_form`),
        each level also offered its dump by field name and the form with its
        fields written where their validation aliases read them. The first the
        annotation reads back equal to the argument is sent, else the first it
        reads as its own validators make of the caller's values (a normalising
        validator), a model in a list, tuple, dict or optional each in its own
        place (`_faithful_through`). When the annotation reads none of them so, the choice is
        made again among the plain dump, the alias form and the form written a
        level at a time from those two, and the first accepted is sent, so a
        form the annotation accepts but reads as other values never replaces
        one it refused; when none is accepted the plain dump is sent and the
        service refuses it. And when the annotation would read a field of what
        is sent as anything other than what its validators make of the
        caller's value (its default, another field's value, or any other), the
        call is refused before sending (`refuse_a_lost_value`), for the
        argument and for each model it holds in a list, a tuple, a dict, an
        optional or an `Annotated`, named by its index or key
        (`_refuse_a_lost_value_through`). See `choose_wire_form`.

        THE SERVICE'S READING IS THE CLIENT'S. The argument is checked, and each form is tried, by
        python mode and then JSON mode, as the service reads a payload (`read_python_then_json`),
        so a strict model that only its alias reads is sent by alias, and a JSON-form dict given for
        a strict model is not refused here when the service would take it.
        """
        adapter = _adapter(annotation)

        def read(candidate: Any) -> Any:
            # How the service reads it: python mode, then JSON mode (`read_python_then_json`).
            return read_python_then_json(candidate, adapter.validate_python, adapter.validate_json)

        try:
            read(value)
        except PydanticValidationError as exc:
            raise RpcValidationError(
                # `errors()` is `list[ErrorDetails]`, a TypedDict; the exception
                # stores plain dicts, and callers read it as JSON.
                details=[dict(e) for e in exc.errors()],
                message=(
                    f"refused before sending: {type(value).__name__} is not a valid "
                    f"{_annotation_name(annotation)}"
                ),
            ) from exc
        is_model_argument = (
            isinstance(value, BaseModel)
            and isinstance(annotation, type)
            and issubclass(annotation, BaseModel)
        )
        faithful = (
            faithful_to(annotation, value)
            if is_model_argument
            else _faithful_through(annotation, value)
        )

        def forms(extra_forms: ExtraForms) -> tuple[Callable[[], Any], ...]:
            dumps: tuple[Callable[[], Any], ...] = (
                lambda: adapter.dump_python(value, mode="json"),
                lambda: adapter.dump_python(value, mode="json", by_alias=True),
            )
            if extra_forms != "none":
                dumps += (lambda: adapter.dump_python(value, mode="json", by_alias=False),)
            declared = annotation if is_model_argument and extra_forms != "none" else None
            return (
                *dumps,
                lambda: nested_form(
                    value, alias_first=False, extra_forms=extra_forms, declared=declared
                ),
            )

        form = choose_wire_form(value, forms("caller checks"), read, faithful)
        if not _reads_back_faithfully(read, annotation, form, value):
            form = choose_wire_form(value, forms("none"), read)
        _refuse_a_lost_value_through(annotation, form, value)
        # A form the receiver refuses is sent as chosen, for the service to refuse; only one it
        # reads is given the fields a subclass instance holds beyond an extra-allowing base.
        try:
            whole = with_subclass_fields_at_open_bases(value, read(form), form)
            if whole is not form:
                read(whole)
        except Exception:
            return form
        return whole

    async def _prepare(
        self, method: str, params: dict[str, Any]
    ) -> tuple[str, bytes, dict[str, str]]:
        """Connect and verify, then the subject, body and headers of a call to `method`. `params`
        are already JSON-ready, via `_encode`."""
        await self._connection()
        if self._verify:
            await self._verify_once()
        body = dict(params)
        subject = self._subject(f"rpc.{method}")
        try:
            encoded = json.dumps(body).encode()
        except (TypeError, ValueError) as exc:
            # Local, and the caller's: nothing has been sent. `_encode` puts
            # arguments through their annotation but does not validate, so a
            # value the annotation does not cover arrives here intact.
            raise RpcClientError(f"an argument to {subject} cannot be encoded: {exc}") from exc
        headers = self._headers_for_send()
        if not any(name.lower() == "content-type" for name in headers):
            # Labelled, so the service reads the header it is documented to read rather than
            # sniffing the bytes, and answers in the encoding this body is in.
            headers["Content-Type"] = CONTENT_TYPE_JSON
        return subject, encoded, headers

    async def _after_validation_reply(self) -> None:
        """Re-verify after the service refused a call's arguments, so a client that is out of date
        raises `ClientOutOfDate` rather than the validation error."""
        if not self._verify:
            return
        # In a rolling deploy, this request may have hit a newer replica
        # with breaking schema changes. Re-verifying surfaces
        # ClientOutOfDate. One describe for the whole cohort that saw the
        # same drift, rather than one each: see `_reverify_together`.
        try:
            await self._reverify_together()
        except ClientOutOfDate:
            raise
        except RpcError as reverify_error:
            # The describe could not be answered (a timeout, a lost connection): that says
            # nothing about the drift, and raising it would tell the caller the service is
            # down when the answer to their call was "your argument is wrong".
            logger.debug(
                f"could not re-verify {self.service!r} after a validation reply "
                f"({type(reverify_error).__name__}); raising the validation error"
            )

    async def _stream(
        self, method: str, params: dict[str, Any], item_type: Any
    ) -> AsyncGenerator[Any]:
        """One streamed call: yield each item `method` sends, validated against `item_type`,
        until its stream ends; raise what the end says when it is an error, with `items`.

        The whole stream is bounded by `timeout` (or what an enclosing request has left), and
        carries that in `Cliffracer-Timeout-Ms`. Leaving the loop early unsubscribes the inbox,
        and the service stops soon after.
        """
        subject, encoded, headers = await self._prepare(method, params)
        timeout = outbound_timeout(self.timeout, subject)
        sent = {
            name: value for name, value in headers.items() if name.lower() != TIMEOUT_HEADER.lower()
        }
        sent[TIMEOUT_HEADER] = header_value(timeout)
        nc = await self._connection()
        try:
            sub = await open_stream(nc, subject, encoded, sent)
        except NatsError as exc:
            raise rpc_error_for(exc, subject, awaiting_reply=True) from exc
        received = 0
        # Held under aclosing, so closing this generator closes the reader at once and with it
        # the reply inbox, rather than when the abandoned reader is finalised.
        reader = read_stream(
            sub,
            subject,
            timeout=timeout,
            item_type=item_type,
            raise_for_envelope=self._raise_for_error,
        )
        try:
            async with contextlib.aclosing(reader) as items:
                async for item in items:
                    received += 1
                    yield item
        except RpcValidationError:
            if received == 0:
                await self._after_validation_reply()
            raise

    async def _call(self, method: str, params: dict[str, Any], return_type: Any) -> Any:
        """One request/reply. `params` are already JSON-ready, via `_encode`."""
        subject, encoded, headers = await self._prepare(method, params)
        reply = await self._request(subject, encoded, headers)
        data = self._decode_reply(reply, subject)
        try:
            self._raise_for_error(data, subject)
        except RpcValidationError:
            await self._after_validation_reply()
            raise
        require_success(data, subject)
        try:
            return _adapter(return_type).validate_python(data.get("result"))
        except PydanticValidationError as exc:
            # The service answered, and the answer is not the shape it declares.
            # `verify()` catches a signature that MOVED; this catches a reply
            # that does not match the signature both sides agree on.
            raise RpcServerError(
                f"{subject} returned a result that does not match its declared type: {exc}"
            ) from exc

    async def __aenter__(self) -> Self:
        """Open the connection, so a bad address is reported here.

        The client is lazy by construction, which is right for one built and
        passed around -- but inside `async with` the block's first line is where
        a caller expects to learn the broker is unreachable, not at whichever
        call happens to run first.
        """
        await self._connection()
        return self

    async def __aexit__(self, *exc_info: object) -> None:
        """Give back a connection this client opened, however the block ended.

        `close()` decides what that means, so the two halves it settled hold
        here too: an owned connection is drained and the client then refuses
        later use, and a BORROWED one is left alone along with the client that
        borrowed it. A context manager that drained someone else's connection
        would be worse than no context manager.

        Exceptions are not suppressed -- returning `None` lets the block's own
        failure propagate, which is the whole reason a caller reaches for this
        instead of a `try`/`finally`.
        """
        await self.close()

    async def close(self) -> None:
        """Drain a connection this client opened, and refuse any later use of it.

        A BORROWED CONNECTION IS LEFT ALONE, and so is the client that borrowed
        it: this client did not open that connection and is not entitled to end
        anyone else's use of it, so such a client keeps working afterwards.

        A CLIENT THAT OWNED ITS CONNECTION REFUSES REUSE rather than opening a
        new one. `close()` is an intent to stop, and reconnecting would make it
        useless for releasing anything -- close/call/close/call would leak a
        connection per round while reading as working code. The flag is set
        whether or not a connection was ever opened, because the intent does not
        depend on that.

        """
        if self._owns_nc:
            # First, so that the client refuses later use whatever happens to the drain, and so that
            # a dial in flight sees it when it completes.
            self._closed = True
            if self._nc is not None and not self._nc.is_closed:
                await self._release(self._nc)

    def _was_closed(self) -> bool:
        """Whether `close()` has run, read again after an await that it may have run during."""
        return self._closed

    @staticmethod
    async def _release(nc: nats.NATS) -> None:
        """Drain a connection this client opened, falling back to closing it, and never raise.

        nats-py refuses a drain while it is redialling (`ConnectionReconnectingError`), which is the
        moment a shutdown path runs, during a broker outage. The connection is then closed without
        the drain: what was buffered cannot be flushed to a broker that is not there. A failure to
        release is not one the caller can act on and must not replace the failure of the block
        that called `close()`, so it is logged and not raised.
        """
        try:
            await nc.drain()
        except Exception as drain_error:
            logger.debug(
                f"could not drain the client's connection ({type(drain_error).__name__}); "
                f"closing it instead"
            )
            try:
                await nc.close()
            except Exception as close_error:
                logger.warning(
                    f"could not close the client's connection ({type(close_error).__name__})"
                )
