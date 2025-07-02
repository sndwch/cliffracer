"""
Unit tests for async RPC functionality
"""

import asyncio
import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import NATSService, ServiceConfig, async_rpc, rpc


class TestAsyncRPC:
    """Test async RPC calling patterns"""

    class TestService(NATSService):
        def __init__(self, config):
            super().__init__(config)
            self.sync_calls = []
            self.async_calls = []

        @rpc
        async def sync_method(self, data: str):
            """Synchronous RPC method"""
            self.sync_calls.append(data)
            return f"sync_response_{data}"

        @async_rpc
        async def async_method(self, data: str):
            """Asynchronous RPC method"""
            self.async_calls.append(data)
            # Note: async methods don't return responses

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_async_service")

    @pytest.fixture
    def service(self, service_config):
        return self.TestService(service_config)

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
        mock_response.data = json.dumps({"result": "test_result"}).encode()
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
        await service._handle_rpc_request(message)

        # Verify method was called
        assert "test_input" in service.sync_calls

        # Verify response was sent
        assert message._response_sent
        response_data = json.loads(message.response_data.decode())
        assert response_data["result"] == "sync_response_test_input"

    @pytest.mark.asyncio
    async def test_handle_async_request(self, service, test_helper):
        """Test handling asynchronous RPC requests"""
        # Create mock message for async request
        message = test_helper.create_mock_message(
            subject="test_async_service.async.async_method", data={"data": "test_input"}
        )

        # Handle the async request
        await service._handle_async_request(message)

        # Verify method was called
        assert "test_input" in service.async_calls

        # Verify no response was sent (async = fire-and-forget)
        assert not message._response_sent

    @pytest.mark.asyncio
    async def test_handle_async_request_unknown_method(self, service, test_helper):
        """Test handling async request for unknown method"""
        # Create mock message for unknown method
        message = test_helper.create_mock_message(
            subject="test_async_service.async.unknown_method", data={"data": "test_input"}
        )

        # Handle the async request (should not raise exception)
        await service._handle_async_request(message)

        # Verify no response was sent and no calls were made
        assert not message._response_sent
        assert len(service.async_calls) == 0
        assert len(service.sync_calls) == 0

    @pytest.mark.asyncio
    async def test_handle_async_request_error(self, service, test_helper):
        """Test error handling in async requests"""

        class ErrorService(NATSService):
            @async_rpc
            async def error_method(self, data: str):
                raise ValueError("Test error")

        error_service = ErrorService(ServiceConfig(name="error_service"))

        # Create mock message
        message = test_helper.create_mock_message(
            subject="error_service.async.error_method", data={"data": "test"}
        )

        # Handle request (should not raise exception, just log error)
        await error_service._handle_async_request(message)

        # Verify no response was sent (errors in async calls are just logged)
        assert not message._response_sent

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

        # Check that async subject subscription was made
        call_args_list = service.nc.subscribe.call_args_list
        subjects = [call[0][0] for call in call_args_list]

        assert "test_async_service.rpc.*" in subjects
        assert "test_async_service.async.*" in subjects


class TestAsyncRPCIntegration:
    """Integration tests for async RPC patterns"""

    @pytest.mark.asyncio
    async def test_sync_vs_async_performance(self):
        """Test that async calls are faster than sync calls"""
        import time

        # Mock service that simulates processing time
        class SlowService(NATSService):
            @rpc
            async def slow_sync_method(self, delay: float = 0.1):
                await asyncio.sleep(delay)
                return "done"

            @async_rpc
            async def slow_async_method(self, delay: float = 0.1):
                await asyncio.sleep(delay)

        service = SlowService(ServiceConfig(name="slow_service"))
        service.nc = AsyncMock()

        # Mock sync response with simulated delay
        async def mock_request(*args, **kwargs):
            await asyncio.sleep(0.01)  # Simulate network delay
            mock_response = AsyncMock()
            mock_response.data = json.dumps({"result": "done"}).encode()
            return mock_response

        service.nc.request = AsyncMock(side_effect=mock_request)
        service.nc.publish = AsyncMock()  # Async calls just publish

        # Test sync calls (sequential, each waits for response)
        start_time = time.time()
        for _ in range(3):
            await service.call_rpc("target", "slow_method", delay=0.01)
        sync_time = time.time() - start_time

        # Test async calls (parallel, no waiting)
        start_time = time.time()
        async_tasks = [service.call_async("target", "slow_method", delay=0.01) for _ in range(3)]
        await asyncio.gather(*async_tasks)
        async_time = time.time() - start_time

        # Async should be much faster (no waiting for responses)
        # Sync should take at least 3 * 0.01 = 0.03 seconds
        # Async should be nearly instant
        assert async_time < sync_time / 2  # Async should be at least 2x faster

        # Verify call patterns
        assert service.nc.request.call_count == 3  # Sync calls use request
        assert service.nc.publish.call_count == 3  # Async calls use publish
