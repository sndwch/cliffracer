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
import json
import uuid
from collections.abc import Mapping
from typing import Any

import nats
from nats.aio.msg import Msg
from nats.errors import NoRespondersError as NoResponders
from nats.errors import TimeoutError as NatsTimeout
from pydantic import TypeAdapter

from cliffracer.core.exceptions import (
    ClientError,
    ClientOutOfDate,
    ClientOutOfDateError,
    RpcClientError,
    RPCError,
    RpcError,
    RpcNoResponders,
    RpcNoRespondersError,
    RpcRefused,
    RpcRefusedError,
    RpcServerError,
    RpcTimeout,
    RPCTimeoutError,
    RpcTimeoutError,
    RpcUnknownMethod,
    RpcUnknownMethodError,
    RpcValidationError,
)
from cliffracer.core.validation import deserialize_payload
from cliffracer.introspect import Description

__all__ = [
    "ServiceClient",
    "RpcError",
    "RpcClientError",
    "RpcServerError",
    "RpcTimeoutError",
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


class ServiceClient:
    """Transport, verification and error mapping for a generated client.

    SERVICE / VERSION / DESCRIPTION_HASH / SIGNATURES are written by the
    generator. `DESCRIPTION_HASH` records which description the file was
    generated from -- provenance for a human and for the generator's own
    regeneration check -- while `SIGNATURES` is what `verify` compares, because
    a per-method hash can name the method that moved and a whole-description
    hash cannot.

    Because `{service}.describe` is subscribed in the RPC queue group, `verify`
    samples a single replica from the running fleet. During a rolling deploy
    with mixed versions, `_call` automatically re-verifies if an
    `RpcValidationError` occurs, detecting schema drift and raising `ClientOutOfDate`.
    """

    SERVICE: str = ""
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
        timeout: float = 30.0,
        headers: dict[str, str] | None = None,
        verify: bool = True,
    ) -> None:
        self._nc = nc
        self._nats_url = nats_url
        self._owns_nc = nc is None
        self._connect_lock = asyncio.Lock()
        self.service = service or self.SERVICE
        self.namespace = namespace
        self.timeout = timeout
        self.headers = dict(headers or {})
        self._verify = verify
        self._verified = False

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
        if self._nc is None:
            async with self._connect_lock:
                if self._nc is None:
                    self._nc = await nats.connect(
                        self._nats_url or "nats://localhost:4222",
                        reconnected_cb=self._on_reconnect,
                    )
        return self._nc

    async def _on_reconnect(self) -> None:
        self._verified = False

    def _headers_for_send(self) -> dict[str, str]:
        """The caller's headers plus a fresh correlation id, for any request.

        Correlation IDs are generated per request so describe calls and
        subsequent RPC invocations maintain distinct tracing identifiers.
        """
        cid = (
            self.headers.get("X-Correlation-ID")
            or self.headers.get("x-correlation-id")
            or self.headers.get("correlation_id")
            or uuid.uuid4().hex
        )
        return {**self.headers, "X-Correlation-ID": cid, "correlation_id": cid}

    def _subject(self, tail: str) -> str:
        base = f"{self.service}.{tail}"
        return f"{self.namespace}.{base}" if self.namespace else base

    async def _request(
        self, subject: str, payload: bytes, headers: dict[str, str] | None = None
    ) -> Msg:
        """Dispatch a NATS request and translate network errors into ClientError exceptions."""
        nc = await self._connection()
        try:
            return await nc.request(subject, payload, timeout=self.timeout, headers=headers)
        except NatsTimeout as exc:
            raise RpcTimeoutError(f"{subject} did not answer within {self.timeout}s") from exc
        except NoResponders as exc:
            raise RpcNoRespondersError(
                f"nothing is subscribed to {subject}; is the service running?"
            ) from exc

    def _raise_for_error(self, data: dict[str, Any], subject: str) -> None:
        """Turn an error envelope into the corresponding exception, or return."""
        if "error" not in data:
            return
        err = str(data["error"])
        if data.get("success") is False and err == "validation failed":
            raise RpcValidationError(data.get("details", []))
        if err.startswith("Unknown method:"):
            raise RpcUnknownMethodError(err)
        if err.startswith("refused: "):
            raise RpcRefusedError(err[len("refused: ") :])
        raise ClientError(f"{subject}: {err}")

    async def verify(self) -> None:
        """Compare this client's per-method hashes with the running service.

        In a multi-replica or rolling deployment, this request samples one replica
        from the RPC queue group. If an individual RPC call subsequently fails with
        an `RpcValidationError`, `_call` re-runs `verify()` to detect whether
        another replica in the fleet has updated its signatures.

        """
        # Send authentication headers with describe requests so services
        # requiring authorization accept the verification request.
        subject = self._subject("describe")
        reply = await self._request(subject, b"", self._headers_for_send())
        data = json.loads(reply.data.decode())
        self._raise_for_error(data, subject)
        live = Description.from_dict(data)
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
            raise ClientOutOfDate(self.service, changed, missing)
        self._verified = True

    def _encode(self, value: Any, annotation: Any) -> Any:
        """JSON-ready value for one argument, through its DECLARED annotation.

        The generated stubs pass the annotation, so a `list[Order]` argument is
        dumped as a list of Order dicts. `TypeAdapter(type(value))` would see
        `list` and lose the item type; that is why the annotation travels from
        the stub rather than being inferred here.
        """
        return TypeAdapter(annotation).dump_python(value, mode="json")

    async def _call(self, method: str, params: dict[str, Any], return_type: Any) -> Any:
        """One request/reply. `params` are already JSON-ready, via `_encode`."""
        await self._connection()
        if self._verify and not self._verified:
            await self.verify()
        body = dict(params)
        subject = self._subject(f"rpc.{method}")
        reply = await self._request(subject, json.dumps(body).encode(), self._headers_for_send())
        reply_h = getattr(reply, "headers", None)
        reply_headers = dict(reply_h) if isinstance(reply_h, Mapping) else {}
        reply_ct = None
        for k, v in reply_headers.items():
            if k.lower() == "content-type":
                reply_ct = v
                break
        data = deserialize_payload(reply.data, content_type=reply_ct, fallback_format="json")
        try:
            self._raise_for_error(data, subject)
        except RpcValidationError:
            if self._verify:
                # In a rolling deploy, this request may have hit a newer replica
                # with breaking schema changes. Re-verifying surfaces ClientOutOfDate.
                await self.verify()
            raise
        if data.get("success") is not True:
            # Replies must include the 'success' key.
            raise ClientError(
                f"protocol error: reply from {self.service}.{method} carries no success key"
            )
        return TypeAdapter(return_type).validate_python(data.get("result"))

    async def close(self) -> None:
        """Drain a connection this client opened. A borrowed one is left alone."""
        if self._owns_nc and self._nc is not None and not self._nc.is_closed:
            await self._nc.drain()
