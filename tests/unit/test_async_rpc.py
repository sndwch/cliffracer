"""
Unit tests for async RPC functionality
"""

import json
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, async_rpc, rpc

pytestmark = pytest.mark.unit


@pytest.fixture
def logged():
    """What the framework logs, as (level, message), for the length of one test.

    loguru does not reach `caplog`, so a sink of our own reads it.
    """
    lines: list[tuple[str, str]] = []
    sink = logger.add(
        lambda m: lines.append((m.record["level"].name, m.record["message"])), level="DEBUG"
    )
    yield lines
    logger.remove(sink)


class TestAsyncRPC:
    """Test async RPC calling patterns"""

    class AsyncRpcSvc(CliffracerService):
        def __init__(self, config):
            super().__init__(config)
            self.sync_calls = []
            self.async_calls = []

        @rpc
        async def sync_method(self, data: str) -> str:
            """Synchronous RPC method"""
            self.sync_calls.append(data)
            return f"sync_response_{data}"

        @async_rpc
        async def async_method(self, data: str) -> None:
            """Asynchronous RPC method"""
            self.async_calls.append(data)
            # Note: async methods don't return responses

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_async_service")

    @pytest.fixture
    def service(self, service_config):
        svc = self.AsyncRpcSvc(service_config)
        # Register decorated handlers (normally done during start())
        svc._discover_handlers()
        return svc

    def test_rpc_decorator(self, service):
        """Test that @rpc decorator sets correct attributes"""
        assert hasattr(service.sync_method, "_cliffracer_rpc")
        assert service.sync_method._cliffracer_rpc is True
        assert service.sync_method.__name__ == "sync_method"
        assert not hasattr(service.sync_method, "_cliffracer_async_rpc")

    def test_async_rpc_decorator(self, service):
        """Test that @async_rpc decorator sets correct attributes"""
        assert hasattr(service.async_method, "_cliffracer_rpc")
        assert service.async_method._cliffracer_rpc is True
        assert service.async_method.__name__ == "async_method"
        assert hasattr(service.async_method, "_cliffracer_async_rpc")
        assert service.async_method._cliffracer_async_rpc is True

    @pytest.mark.asyncio
    async def test_call_rpc_sync(self, service):
        """Test synchronous RPC call"""
        # Mock NATS connection
        service.nc = AsyncMock()
        mock_response = AsyncMock()
        mock_response.data = json.dumps({"success": True, "result": "test_result"}).encode()
        service.nc.request = AsyncMock(return_value=mock_response)

        # Call RPC method
        result = await service.call_rpc("target_service", "test_method", data="test")

        # Verify NATS request was called correctly
        service.nc.request.assert_called_once()
        call_args = service.nc.request.call_args

        assert call_args[0][0] == "target_service.rpc.test_method"  # subject
        payload = json.loads(call_args[0][1].decode())
        assert payload["data"] == "test"
        assert "correlation_id" in payload  # correlation_id is now included
        assert call_args[1]["timeout"] == service.config.request_timeout

        # Verify result
        assert result == "test_result"

    @pytest.mark.asyncio
    async def test_call_async(self, service):
        """Test asynchronous RPC call"""
        # Mock NATS connection
        service.nc = AsyncMock()

        # Call async method
        await service.call_async("target_service", "test_method", data="test")

        # Verify NATS publish was called correctly (no request/response)
        service.nc.publish.assert_called_once()
        call_args = service.nc.publish.call_args

        assert call_args[0][0] == "target_service.async.test_method"  # subject
        payload = json.loads(call_args[0][1].decode())
        assert payload["data"] == "test"
        assert "correlation_id" in payload  # correlation_id is now included

    @pytest.mark.asyncio
    async def test_call_rpc_no_wait(self, service):
        """Test RPC no-wait call"""
        # Mock NATS connection
        service.nc = AsyncMock()

        # Call RPC method without waiting
        await service.call_rpc_no_wait("target_service", "test_method", data="test")

        # Verify NATS publish was called correctly
        service.nc.publish.assert_called_once()
        call_args = service.nc.publish.call_args

        assert call_args[0][0] == "target_service.rpc.test_method"  # subject
        payload = json.loads(call_args[0][1].decode())
        assert payload["data"] == "test"
        assert "correlation_id" in payload  # correlation_id is now included

    @pytest.mark.asyncio
    async def test_handle_rpc_request_sync(self, service, test_helper):
        """Test handling synchronous RPC requests"""
        # Create mock message that expects response
        message = test_helper.create_mock_message(
            subject="test_async_service.rpc.sync_method", data={"data": "test_input"}
        )

        # Handle the request
        await service.container._handle_rpc_request(message)

        # Verify method was called
        assert "test_input" in service.sync_calls

        # Verify response was sent
        assert message.responded_data is not None
        response_data = json.loads(message.responded_data.decode())
        assert response_data["result"] == "sync_response_test_input"

    @pytest.mark.asyncio
    async def test_handle_rpc_request_no_reply_subject(self, service, test_helper):
        """The handler runs and the dispatcher does not attempt a reply.

        Asserted on the call count, not on `responded_data`. The mock refuses a
        reply when there is no reply subject, as the real `Msg` does, and every
        `msg.respond` in the dispatcher sits inside `except Exception` with a
        debug log -- so a wrongly attempted reply leaves `responded_data` at
        None and is indistinguishable from no attempt. Measured: with the
        dispatcher's `has_reply` guard removed, the `responded_data` form of
        this test passes.
        """
        message = test_helper.create_mock_message(
            subject="test_async_service.rpc.sync_method", data={"data": "test_input"}, reply=None
        )

        await service.container._handle_rpc_request(message)

        assert "test_input" in service.sync_calls
        assert message.respond_calls == 0, (
            "the dispatcher attempted a reply on a message with no reply subject"
        )
        assert message.responded_data is None

    @pytest.mark.asyncio
    async def test_handle_rpc_request_unknown_method_no_reply(self, service, test_helper):
        """An unknown method with no reply subject: no attempt, and no raise.

        The error path has its own `has_reply` guard, so this is the same
        property as the test above on a different branch -- and the same reason
        for counting attempts rather than reading `responded_data`.
        """
        message = test_helper.create_mock_message(
            subject="test_async_service.rpc.unknown_method", data={"data": "test_input"}, reply=None
        )

        await service.container._handle_rpc_request(message)

        assert message.respond_calls == 0, (
            "the dispatcher attempted an error reply on a message with no reply subject"
        )
        assert message.responded_data is None

    @pytest.mark.asyncio
    async def test_handle_async_request(self, service, test_helper):
        """Test handling asynchronous RPC requests"""
        # Create mock message for async request
        message = test_helper.create_mock_message(
            subject="test_async_service.async.async_method", data={"data": "test_input"}
        )

        # Handle the async request
        await service.container._handle_async_request(message)

        # Verify method was called
        assert "test_input" in service.async_calls

        # Verify no response was sent (async = fire-and-forget). `respond_calls`
        # counts attempts; `responded_data` alone stays None after a refused one.
        assert message.respond_calls == 0

    @pytest.mark.asyncio
    async def test_handle_async_request_unknown_method(self, service, test_helper, logged):
        """Test handling async request for unknown method"""
        # Create mock message for unknown method
        message = test_helper.create_mock_message(
            subject="test_async_service.async.unknown_method", data={"data": "test_input"}
        )

        # Handle the async request (should not raise exception)
        await service.container._handle_async_request(message)

        # Verify no response was sent and no calls were made, and that the
        # caller's mistake was reported rather than swallowed.
        assert message.respond_calls == 0
        assert len(service.async_calls) == 0
        assert len(service.sync_calls) == 0
        assert ("WARNING", "Unknown async method: unknown_method") in logged

    @pytest.mark.asyncio
    async def test_handle_async_request_error(self, service, test_helper, logged):
        """A failing async handler is reported with its name and cause, and nobody is answered."""

        class ErrorService(CliffracerService):
            @async_rpc
            async def error_method(self, data: str) -> str:
                raise ValueError("Test error")

        error_service = ErrorService(ServiceConfig(name="error_service"))
        error_service._discover_handlers()

        # Create mock message
        message = test_helper.create_mock_message(
            subject="error_service.async.error_method", data={"data": "test"}
        )

        # Handle request (should not raise exception, just log error)
        await error_service.container._handle_async_request(message)

        # Nothing answers a fire-and-forget request, so the log is the only
        # place the failure can be seen: it must name the method and the cause.
        errors = [m for level, m in logged if level == "ERROR"]
        assert len(errors) == 1, errors
        assert errors[0].startswith("Error handling async request error_method (correlation_id: ")
        assert errors[0].endswith("): Test error")
        assert message.respond_calls == 0

    @pytest.mark.asyncio
    async def test_service_startup_subscribes_to_async(self, service):
        """Test that service startup subscribes to async subjects"""
        # Mock NATS connection completely
        service.nc = AsyncMock()
        service.nc.is_closed = False

        # Mock subscribe calls
        mock_subscription = AsyncMock()
        service.nc.subscribe = AsyncMock(return_value=mock_subscription)

        # Mock only the connect method to prevent actual NATS connection
        service.connect = AsyncMock()

        # Start service - this will test the subscription logic
        await service.start()

        # Verify connection was attempted
        service.connect.assert_called_once()

        # Verify subscriptions were created
        assert service.nc.subscribe.call_count >= 2  # At least RPC and async

        # Stop service to terminate subscription tasks.
        await service.stop()

        # Check that async subject subscription was made
        call_args_list = service.nc.subscribe.call_args_list
        subjects = [call[0][0] for call in call_args_list]

        assert "test_async_service.rpc.*" in subjects
        assert "test_async_service.async.*" in subjects
