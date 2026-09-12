"""Unit tests for RPC traceback sanitization by default.

Verifies that unhandled exceptions do not leak stack traces or internal
error details over the broker wire unless expose_internal_errors=True.
"""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc

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
async def test_rpc_logs_full_traceback_server_side(monkeypatch: pytest.MonkeyPatch) -> None:
    """Server-side logger logs the exception and traceback regardless of wire setting."""
    cfg = ServiceConfig(name="crash_svc", expose_internal_errors=False)
    svc = CrashyService(cfg)
    svc._discover_handlers()

    mock_logger = MagicMock()
    svc.container.dispatcher.rpc.logger = mock_logger

    msg = MockRpcMsg(
        subject="crash_svc.crash_runtime",
        data=json.dumps({}).encode(),
        headers={"X-Correlation-ID": "corr-log-check"},
    )

    await svc.container._handle_rpc_request(msg)

    # Logger.exception should be called with correlation id and handler name
    assert mock_logger.exception.called
    call_args = mock_logger.exception.call_args[0][0]
    assert "crash_runtime" in call_args
    assert "corr-log-check" in call_args
