"""Asking a running service over the broker: the dial, the describe, and reading what comes back.

Shared by `cliffracer-generate-client` and by `cliffracer describe` / `cliffracer call`, so that
the three ask a service the same way: the same bounded dial, the same reading of a broker that
refused this client (`BrokerRefused`, from nats-py's error callback), and the same check of a
describe reply's shape before anything indexes it.
"""

from __future__ import annotations

import os
from typing import Any

from nats.errors import AuthorizationError
from nats.errors import Error as NatsError
from nats.errors import TimeoutError as NatsTimeoutError

from cliffracer.core import dial
from cliffracer.core.discovery import HandlerDiscovery

DEFAULT_URL = "nats://localhost:4222"


def resolve_nats_url(flag: str | None, environ: dict[str, str]) -> str:
    """Flag, then `$CLIFFRACER_NATS_URL`, then the default. One rule, testable."""
    return flag or environ.get("CLIFFRACER_NATS_URL") or DEFAULT_URL


def describe_subject(service: str, namespace: str | None = None) -> str:
    """The subject a service answers `describe` on.

    Built here rather than spelled at the call site, for the same reason the
    service builds its own through a helper: the environment prefix goes
    outside the namespace, and a subject assembled inline gets neither. The CLI
    has no ServiceConfig, so the prefix comes from the environment variable that
    the config field defaults from.
    """
    return HandlerDiscovery.scoped_subject(
        f"{service}.describe",
        namespace=namespace,
        subject_prefix=os.environ.get("CLIFFRACER_SUBJECT_PREFIX") or None,
    )


class BrokerRefused(NatsError):
    """The broker was reached and refused this client: its credentials or its permissions."""

    def __init__(self, kind: str, detail: str) -> None:
        super().__init__(detail)
        self.kind = kind
        self.detail = detail


class _Refusals:
    """What the broker said, through nats-py's error callback, about this client being refused.

    A refused login ends the dial with `NoServersError`, and a subscription the client's role does
    not allow is reported only here while the request waits out its timeout: neither carries the
    broker's reason, so without this both read as a broker that is not there or a service that did
    not answer. Every other error is dropped, which also keeps nats-py's default callback from
    printing a traceback for each.
    """

    def __init__(self) -> None:
        self.credentials: str | None = None
        self.permissions: str | None = None

    async def record(self, error: Exception) -> None:
        """nats-py's `error_cb`; it takes coroutine functions, not callable objects."""
        text = str(error)
        lowered = text.lower()
        if isinstance(error, AuthorizationError) or "authorization violation" in lowered:
            self.credentials = self.credentials or text or "authorization violation"
        elif "permissions violation" in lowered:
            self.permissions = self.permissions or text


async def fetch_description(
    nats_url: str,
    service: str,
    namespace: str | None,
    timeout: float,
    headers: dict[str, str] | None = None,
    inbox_prefix: str | None = None,
) -> bytes:
    """The raw bytes a service answered `describe` with.

    Only the exchange with the broker happens here. Decoding is the caller's,
    so that a reply that is not JSON cannot raise inside the same `try` as a
    refused connection and be reported as one.
    """
    refusals = _Refusals()
    options: dict[str, Any] = {} if inbox_prefix is None else {"inbox_prefix": inbox_prefix}

    # The whole dial is bounded by `timeout`: `connect_timeout` bounds each attempt, and the one
    # reconnect attempt would otherwise take a second attempt's worth of time on top of it. The
    # framework's dial closes the client a cut-off leaves, which `wait_for(nats.connect(...))`
    # leaves to the garbage collector.
    try:
        nc = await dial.connect(
            nats_url,
            timeout=timeout,
            connect_timeout=timeout,
            max_reconnect_attempts=1,
            reconnect_time_wait=min(timeout, 0.5),
            error_cb=refusals.record,
            **options,
        )
    except Exception as exc:
        if refusals.credentials is not None:
            raise BrokerRefused("credentials", refusals.credentials) from exc
        raise
    try:
        subject = describe_subject(service, namespace)
        try:
            reply = await nc.request(subject, b"", timeout=timeout, headers=headers)
        except NatsTimeoutError as exc:
            if refusals.permissions is not None:
                raise BrokerRefused("permissions", refusals.permissions) from exc
            raise
        return bytes(reply.data)
    finally:
        # `close`, not `drain`: one request inbox is all there is to flush, and `drain` raises on
        # a connection that has already dropped, which would replace the error `main` sorts into
        # exit 2 or 3 with one it did not mean to report.
        await nc.close()


_JSON_KINDS = ((bool, "boolean"), (dict, "object"), (list, "array"), (str, "string"))


def _json_kind(value: object) -> str:
    if value is None:
        return "null"
    for tp, name in _JSON_KINDS:
        if isinstance(value, tp):
            return name
    return "number" if isinstance(value, int | float) else type(value).__name__


def _a(kind: str) -> str:
    """`kind` with its article: "an object", "a string", and "null" bare."""
    if kind == "null":
        return kind
    return f"{'an' if kind[0] in 'aeiou' else 'a'} {kind}"


# The keys each TypeRef kind is read by. A kind not listed here is left to the
# emitter, which owns the vocabulary and refuses an unknown one by name.
_TYPE_REF_KEYS: dict[str, dict[str, type]] = {
    "scalar": {"name": str},
    "model": {"module": str, "qualname": str},
    "list": {"item": dict},
    "dict": {"value": dict},
    "optional": {"inner": dict},
    "literal": {"values": list},
}


def _field_problem(parent: dict[str, Any], key: str, path: str, expected: type) -> str | None:
    if key not in parent:
        return f"{path} is missing"
    kind, want = _json_kind(parent[key]), _json_kind(expected())
    return None if kind == want else f"{path} is {_a(kind)}, not {_a(want)}"


def _type_ref_problem(ref: dict[str, Any], path: str) -> str | None:
    if problem := _field_problem(ref, "kind", f"{path}.kind", str):
        return problem
    for key, expected in _TYPE_REF_KEYS.get(ref["kind"], {}).items():
        if problem := _field_problem(ref, key, f"{path}.{key}", expected):
            return problem
        if expected is dict and (problem := _type_ref_problem(ref[key], f"{path}.{key}")):
            return problem
    return None


def _items_problem(parent: dict[str, Any], key: str, path: str) -> str | None:
    """An optional list of objects: absent is fine, anything else must be one."""
    if key not in parent:
        return None
    if problem := _field_problem(parent, key, path, list):
        return problem
    for index, item in enumerate(parent[key]):
        if not isinstance(item, dict):
            return f"{path}[{index}] is {_a(_json_kind(item))}, not an object"
    return None


def _reply_problem(body: object) -> str | None:
    """Where a describe reply stops being a description, or None if it is one.

    The reply is whatever answered on the describe subject, and every reader
    after this indexes the keys it expects. So the shape those readers depend
    on is checked here, explicitly, and the first place it fails is named. It
    is not a broad `except` around the readers: that would report a defect in
    the generator, on a reply that was fine, as the responder's fault.
    """
    if not isinstance(body, dict):
        return f"the reply is {_a(_json_kind(body))}, not an object"
    for key in ("service", "version"):
        if problem := _field_problem(body, key, key, str):
            return problem
    for key in ("methods", "listeners", "streams"):
        if problem := _items_problem(body, key, key):
            return problem
    for index, method in enumerate(body.get("methods", [])):
        path = f"methods[{index}]"
        for key, expected in (("name", str), ("returns", dict)):
            if problem := _field_problem(method, key, f"{path}.{key}", expected):
                return problem
        if problem := _type_ref_problem(method["returns"], f"{path}.returns"):
            return problem
        if problem := _items_problem(method, "params", f"{path}.params"):
            return problem
        for p_index, param in enumerate(method.get("params", [])):
            p_path = f"{path}.params[{p_index}]"
            for key, expected in (("name", str), ("type", dict)):
                if problem := _field_problem(param, key, f"{p_path}.{key}", expected):
                    return problem
            if problem := _type_ref_problem(param["type"], f"{p_path}.type"):
                return problem
    for index, stream in enumerate(body.get("streams", [])):
        if problem := _field_problem(stream, "name", f"streams[{index}].name", str):
            return problem
    return None
