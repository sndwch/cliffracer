"""Unit tests for wire error sanitization by default.

Verifies that unhandled exceptions do not leak stack traces or internal
error details over the broker wire unless expose_internal_errors=True.

The claim is about the wire, so the tests cover both paths that answer on it.
For a long time they covered only `{service}.rpc.*`, and the sentence above was
read as a property of the process while `{service}.describe` published its
exception verbatim in both flag positions. Describe is the wider surface of the
two: every service subscribes it and it is answered with no authentication, so
the tests for it are here beside the claim rather than in a file of their own.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.testing import refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit


class MockRpcMsg:
    """Mock NATS message for driving RPC dispatch."""

    def __init__(
        self,
        subject: str,
        data: bytes,
        headers: dict[str, str] | None = None,
        reply: str = "_INBOX.test_reply",
    ) -> None:
        self.subject = subject
        self.data = data
        self.headers = headers or {}
        self.reply = reply
        self.response_bytes: bytes | None = None
        self.response_headers: dict[str, str] | None = None

    async def respond(self, data: bytes) -> None:
        refuse_a_reply_with_no_subject(self)
        self.response_bytes = data
        self.response_headers = dict(self.headers)


class CustomInternalError(Exception):
    """Custom exception simulating an unhandled internal failure."""


class CrashyService(CliffracerService):
    @rpc
    async def crash_runtime(self) -> str:
        raise RuntimeError("database credentials in /var/secrets/db.key failed")

    @rpc
    async def crash_custom(self, code: int) -> int:
        raise CustomInternalError(f"internal secret token {code} is invalid")

    @rpc
    async def divide(self, a: int, b: int) -> int:
        return a // b


@pytest.mark.asyncio
async def test_rpc_traceback_sanitized_by_default() -> None:
    """By default, expose_internal_errors is False: wire response contains no traceback."""
    cfg = ServiceConfig(name="crash_svc")
    assert cfg.expose_internal_errors is False

    svc = CrashyService(cfg)
    svc._discover_handlers()

    msg = MockRpcMsg(
        subject="crash_svc.crash_runtime",
        data=json.dumps({}).encode(),
        headers={"X-Correlation-ID": "corr-safe-001"},
    )

    await svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())

    assert reply["success"] is False
    assert "traceback" not in reply
    assert reply["error"] == "Internal server error (correlation_id: corr-safe-001)"
    assert reply["correlation_id"] == "corr-safe-001"
    assert "timestamp" in reply


@pytest.mark.asyncio
async def test_rpc_custom_exception_sanitized_by_default() -> None:
    """Custom exceptions are also sanitized to correlation_id message without traceback."""
    cfg = ServiceConfig(name="crash_svc")
    svc = CrashyService(cfg)
    svc._discover_handlers()

    msg = MockRpcMsg(
        subject="crash_svc.crash_custom",
        data=json.dumps({"code": 12345}).encode(),
        headers={"X-Correlation-ID": "corr-custom-999"},
    )

    await svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())

    assert reply["success"] is False
    assert "traceback" not in reply
    assert "internal secret token" not in reply["error"]
    assert reply["error"] == "Internal server error (correlation_id: corr-custom-999)"
    assert reply["correlation_id"] == "corr-custom-999"


@pytest.mark.asyncio
async def test_rpc_zero_division_sanitized_by_default() -> None:
    """ZeroDivisionError is sanitized when expose_internal_errors is False."""
    cfg = ServiceConfig(name="crash_svc")
    svc = CrashyService(cfg)
    svc._discover_handlers()

    msg = MockRpcMsg(
        subject="crash_svc.divide",
        data=json.dumps({"a": 10, "b": 0}).encode(),
        headers={"X-Correlation-ID": "corr-div-0"},
    )

    await svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())

    assert reply["success"] is False
    assert "traceback" not in reply
    assert reply["error"] == "Internal server error (correlation_id: corr-div-0)"
    assert reply["correlation_id"] == "corr-div-0"


@pytest.mark.asyncio
async def test_rpc_traceback_included_when_opted_in() -> None:
    """When expose_internal_errors=True, wire response includes traceback and original error."""
    cfg = ServiceConfig(name="crash_svc", expose_internal_errors=True)
    assert cfg.expose_internal_errors is True

    svc = CrashyService(cfg)
    svc._discover_handlers()

    msg = MockRpcMsg(
        subject="crash_svc.crash_runtime",
        data=json.dumps({}).encode(),
        headers={"X-Correlation-ID": "corr-debug-002"},
    )

    await svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())

    assert reply["success"] is False
    assert "traceback" in reply
    assert "RuntimeError" in reply["traceback"]
    assert "database credentials in /var/secrets/db.key failed" in reply["error"]
    assert reply["correlation_id"] == "corr-debug-002"
    assert "timestamp" in reply


@pytest.mark.asyncio
async def test_rpc_logs_full_traceback_server_side() -> None:
    """Server-side logger logs the exception and traceback regardless of wire setting.

    Read from a real loguru sink, not a mock logger: what is asserted is the record that reaches
    a sink, with its exception attached, which a mock's `exception` method never produces."""
    cfg = ServiceConfig(name="crash_svc", expose_internal_errors=False)
    svc = CrashyService(cfg)
    svc._discover_handlers()

    records: list[Any] = []
    sink = logger.add(lambda message: records.append(message.record), level="ERROR")
    msg = MockRpcMsg(
        subject="crash_svc.crash_runtime",
        data=json.dumps({}).encode(),
        headers={"X-Correlation-ID": "corr-log-check"},
    )
    try:
        await svc.container._handle_rpc_request(msg)
    finally:
        logger.remove(sink)

    (record,) = [r for r in records if "crash_runtime" in r["message"]]
    # The line names the handler and the request, and carries the error text the wire withholds.
    assert "corr-log-check" in record["message"]
    assert "database credentials in /var/secrets/db.key failed" in record["message"]
    # And the exception itself is attached, with its traceback, for any sink that formats it.
    assert record["exception"] is not None
    assert record["exception"].type is RuntimeError
    assert record["exception"].traceback is not None
    # The wire reply, by contrast, says none of it.
    reply = json.loads(msg.response_bytes.decode())
    assert "database credentials" not in json.dumps(reply)
    assert "traceback" not in reply


@pytest.mark.asyncio
async def test_describe_error_sanitized_by_default() -> None:
    """The describe path answers on the same wire and reads the same flag."""
    import cliffracer.introspect

    original = cliffracer.introspect.describe

    def exploding(*args: object, **kwargs: object) -> object:
        raise RuntimeError("schema cache at /etc/cliffracer/secrets/db.key unreadable")

    cliffracer.introspect.describe = exploding  # type: ignore[assignment]
    try:
        cfg = ServiceConfig(name="crash_svc")
        assert cfg.expose_internal_errors is False
        svc = CrashyService(cfg)
        svc._discover_handlers()

        msg = MockRpcMsg(
            subject="crash_svc.describe", data=b"", headers={"X-Correlation-ID": "corr-desc-hide"}
        )
        await svc.container._handle_describe_request(msg)
    finally:
        cliffracer.introspect.describe = original  # type: ignore[assignment]

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    body = json.dumps(reply)
    assert "/etc/cliffracer/secrets/db.key" not in body, body
    assert "RuntimeError" not in body, body
    assert reply["success"] is False
    assert reply["error"] == "Internal server error (correlation_id: corr-desc-hide)"
    # The gate changes the TEXT; the typed code still says what class this is.
    # These two landed on the same envelope from different changes, so the
    # seam is asserted rather than left to whichever merged second.
    assert reply["code"] == "internal", reply


@pytest.mark.asyncio
async def test_describe_error_exposed_when_opted_in() -> None:
    """CONTROL. The gate opens, so the flag governs rather than deletes."""
    import cliffracer.introspect

    original = cliffracer.introspect.describe

    def exploding(*args: object, **kwargs: object) -> object:
        raise RuntimeError("schema cache at /etc/cliffracer/secrets/db.key unreadable")

    cliffracer.introspect.describe = exploding  # type: ignore[assignment]
    try:
        svc = CrashyService(ServiceConfig(name="crash_svc", expose_internal_errors=True))
        svc._discover_handlers()

        msg = MockRpcMsg(
            subject="crash_svc.describe", data=b"", headers={"X-Correlation-ID": "corr-desc-show"}
        )
        await svc.container._handle_describe_request(msg)
    finally:
        cliffracer.introspect.describe = original  # type: ignore[assignment]

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    assert reply["error"] == "schema cache at /etc/cliffracer/secrets/db.key unreadable"
    assert reply["code"] == "internal", reply


@pytest.mark.asyncio
async def test_a_describe_refusal_is_still_delivered_whole() -> None:
    """CONTROL against over-reach, and the reason this arm is gated alone.

    Describe has two error arms. The other one answers a `RejectMessage`, whose
    reason an extension authored to be read -- and where an exception's text
    does reach it, that is gated at the pipeline instead. Masking this arm must
    not touch that one.
    """
    from cliffracer.core.extension import Extension, RejectMessage

    class Refuser(Extension):
        fails_closed = True

        async def worker_setup(self, ctx: object) -> None:
            raise RejectMessage("introspection unauthorized")

    class GuardedService(CliffracerService):
        guard = Refuser()

    svc = GuardedService(ServiceConfig(name="crash_svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = MockRpcMsg(
        subject="crash_svc.describe", data=b"", headers={"X-Correlation-ID": "corr-desc-refuse"}
    )
    await svc.container._handle_describe_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    assert reply["success"] is False
    assert reply["error"] == "refused: introspection unauthorized"
    # And a refusal is still labelled a refusal, not folded into internal.
    assert reply["code"] == "refused", reply
