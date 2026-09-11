"""Unit tests for RPC error envelope alignment.

Verifies that all RPC error outcomes guarantee "success": False, including
unknown method, validation failure, policy refusal (RejectMessage),
unhandled exceptions, and describe request errors.
"""

import json
from datetime import datetime
from typing import Annotated
from unittest.mock import AsyncMock

import pytest
from pydantic import Field

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    rpc,
)
from cliffracer.client import (
    ClientError,
    RpcRefused,
    RpcUnknownMethod,
    RpcValidationError,
    ServiceClient,
)
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext


class MockRpcMsg:
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


class SampleRpcService(CliffracerService):
    @rpc
    async def calculate(self, x: int, y: Annotated[int, Field(gt=0)]) -> int:
        return x // y

    @rpc
    async def crash(self) -> str:
        raise RuntimeError("simulated server crash")


@pytest.fixture
def rpc_service() -> SampleRpcService:
    svc = SampleRpcService(ServiceConfig(name="math_svc", version="1.0.0"))
    svc._discover_handlers()
    return svc


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rpc_unknown_method_error_envelope(rpc_service: SampleRpcService) -> None:
    """Unknown method reply guarantees success: False, error, timestamp, correlation_id."""
    msg = MockRpcMsg(
        subject="math_svc.nonexistent_method",
        data=json.dumps({}).encode(),
        headers={"X-Correlation-ID": "corr-unknown-1"},
    )

    await rpc_service.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    assert reply["success"] is False
    assert "Unknown method: nonexistent_method" in reply["error"]
    assert reply["correlation_id"] == "corr-unknown-1"

    ts = datetime.fromisoformat(reply["timestamp"])
    assert ts.tzinfo is not None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rpc_unknown_method_without_reply_drops_cleanly(
    rpc_service: SampleRpcService,
) -> None:
    """Unknown method with no reply inbox does not attempt to respond and does not raise."""
    msg = MockRpcMsg(
        subject="math_svc.nonexistent_method",
        data=json.dumps({}).encode(),
        reply="",
    )

    await rpc_service.container._handle_rpc_request(msg)
    assert msg.response_bytes is None


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rpc_validation_failure_error_envelope(rpc_service: SampleRpcService) -> None:
    """Validation failure reply guarantees success: False, error, details, timestamp, correlation_id."""
    # y must be > 0; passing y=-5 causes validation failure
    msg = MockRpcMsg(
        subject="math_svc.calculate",
        data=json.dumps({"x": 10, "y": -5}).encode(),
        headers={"X-Correlation-ID": "corr-val-1"},
    )

    await rpc_service.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    assert reply["success"] is False
    assert reply["error"] == "validation failed"
    assert isinstance(reply["details"], list)
    assert len(reply["details"]) > 0
    assert reply["correlation_id"] == "corr-val-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rpc_payload_decode_failure_error_envelope(rpc_service: SampleRpcService) -> None:
    """Invalid payload bytes guarantee success: False with validation failed error envelope."""
    msg = MockRpcMsg(
        subject="math_svc.calculate",
        data=b"not valid json {{{",
        headers={"X-Correlation-ID": "corr-decode-1"},
    )

    await rpc_service.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    assert reply["success"] is False
    assert reply["error"] == "validation failed"
    assert reply["details"][0]["type"] == "payload_invalid"
    assert reply["correlation_id"] == "corr-decode-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rpc_reject_message_error_envelope() -> None:
    """RejectMessage raised by extension guarantees success: False in reply."""

    class BlockingExtension(Extension):
        async def worker_setup(self, ctx: WorkerContext) -> None:
            if ctx.kind == "rpc":
                raise RejectMessage("rate limit exceeded")

    class RefusingRpcService(CliffracerService):
        blocker = BlockingExtension()

        @rpc
        async def calculate(self, x: int, y: int) -> int:
            return x + y

    svc = RefusingRpcService(ServiceConfig(name="math_svc", version="1.0.0"))
    svc._discover_handlers()

    msg = MockRpcMsg(
        subject="math_svc.calculate",
        data=json.dumps({"x": 10, "y": 2}).encode(),
        headers={"X-Correlation-ID": "corr-block-1"},
    )

    await svc.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    assert reply["success"] is False
    assert reply["error"] == "refused: rate limit exceeded"
    assert reply["correlation_id"] == "corr-block-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_rpc_unhandled_exception_error_envelope(rpc_service: SampleRpcService) -> None:
    """Unhandled exception in handler guarantees success: False with traceback."""
    msg = MockRpcMsg(
        subject="math_svc.crash",
        data=json.dumps({}).encode(),
        headers={"X-Correlation-ID": "corr-crash-1"},
    )

    await rpc_service.container._handle_rpc_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    assert reply["success"] is False
    assert "Internal server error" in reply["error"]
    assert "traceback" not in reply
    assert reply["correlation_id"] == "corr-crash-1"

    # Opt-in with expose_internal_errors=True
    opt_service = SampleRpcService(ServiceConfig(name="math_svc_opt", expose_internal_errors=True))
    opt_service._discover_handlers()
    msg_opt = MockRpcMsg(
        subject="math_svc_opt.crash",
        data=json.dumps({}).encode(),
        headers={"X-Correlation-ID": "corr-crash-opt"},
    )
    await opt_service.container._handle_rpc_request(msg_opt)
    assert msg_opt.response_bytes is not None
    reply_opt = json.loads(msg_opt.response_bytes.decode())
    assert reply_opt["success"] is False
    assert "simulated server crash" in reply_opt["error"]
    assert "traceback" in reply_opt
    assert reply_opt["correlation_id"] == "corr-crash-opt"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_describe_request_refusal_error_envelope() -> None:
    """RejectMessage in describe request guarantees success: False."""

    class AuthBlockExtension(Extension):
        async def worker_setup(self, ctx: WorkerContext) -> None:
            if ctx.kind == "describe":
                raise RejectMessage("introspection unauthorized")

    class RefusingDescribeService(CliffracerService):
        auth_block = AuthBlockExtension()

    svc = RefusingDescribeService(ServiceConfig(name="math_svc", version="1.0.0"))
    svc._discover_handlers()

    msg = MockRpcMsg(
        subject="math_svc.describe",
        data=b"",
        headers={"X-Correlation-ID": "corr-desc-1"},
    )

    await svc.container._handle_describe_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    assert reply["success"] is False
    assert reply["error"] == "refused: introspection unauthorized"
    assert reply["correlation_id"] == "corr-desc-1"


@pytest.mark.unit
@pytest.mark.asyncio
async def test_describe_request_exception_error_envelope(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unhandled exception in describe request guarantees success: False."""

    def mock_describe(*args: object, **kwargs: object) -> object:
        raise RuntimeError("describe exploded")

    monkeypatch.setattr("cliffracer.introspect.describe", mock_describe)

    svc = SampleRpcService(ServiceConfig(name="math_svc", version="1.0.0"))
    svc._discover_handlers()

    msg = MockRpcMsg(
        subject="math_svc.describe",
        data=b"",
        headers={"X-Correlation-ID": "corr-desc-err"},
    )

    await svc.container._handle_describe_request(msg)

    assert msg.response_bytes is not None
    reply = json.loads(msg.response_bytes.decode())
    assert reply["success"] is False
    assert "describe exploded" in reply["error"]
    assert reply["correlation_id"] == "corr-desc-err"


@pytest.mark.unit
def test_client_raise_for_error_interop() -> None:
    """ServiceClient correctly maps error responses with success: False to typed exceptions."""
    client = ServiceClient(service="math_svc")

    with pytest.raises(RpcUnknownMethod) as exc_unknown:
        client._raise_for_error(
            {"success": False, "error": "Unknown method: foo", "correlation_id": "c1"},
            "math_svc.foo",
        )
    assert "Unknown method: foo" in str(exc_unknown.value)

    with pytest.raises(RpcRefused) as exc_refused:
        client._raise_for_error(
            {"success": False, "error": "refused: quota exceeded", "correlation_id": "c2"},
            "math_svc.foo",
        )
    assert "quota exceeded" in str(exc_refused.value)

    with pytest.raises(RpcValidationError) as exc_val:
        client._raise_for_error(
            {
                "success": False,
                "error": "validation failed",
                "details": [{"loc": ["x"], "msg": "required"}],
                "correlation_id": "c3",
            },
            "math_svc.foo",
        )
    assert len(exc_val.value.details) == 1

    with pytest.raises(ClientError) as exc_server:
        client._raise_for_error(
            {"success": False, "error": "internal crash", "correlation_id": "c4"},
            "math_svc.foo",
        )
    assert "internal crash" in str(exc_server.value)


@pytest.mark.unit
@pytest.mark.asyncio
async def test_client_enforces_success_key_present() -> None:
    """ServiceClient raises protocol ClientError when reply completely lacks success key."""
    mock_nc = AsyncMock()
    mock_reply = MockRpcMsg(
        subject="_INBOX.test",
        data=json.dumps({"result": 42}).encode(),
        headers={"Content-Type": "application/json"},
    )
    mock_nc.request = AsyncMock(return_value=mock_reply)

    client = ServiceClient(nc=mock_nc, service="math_svc", verify=False)
    with pytest.raises(ClientError) as exc:
        await client._call("calculate", {"x": 1, "y": 2}, int)
    assert "protocol error: reply from math_svc.calculate carries no success key" in str(exc.value)
