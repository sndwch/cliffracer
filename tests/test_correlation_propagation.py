"""
Comprehensive tests for correlation ID propagation across Cliffracer services.

This test suite verifies that correlation IDs are properly:
- Generated when not present
- Extracted from incoming requests
- Propagated through service calls
- Included in responses
- Logged consistently
"""

import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient

from cliffracer import (
    CliffracerService,
    CorrelationContext,
    HTTPNATSService,
    ServiceConfig,
    create_correlation_id,
    get_correlation_id,
    rpc,
    set_correlation_id,
    with_correlation_id,
)
from cliffracer.middleware.correlation import correlation_id_dependency


class TestCorrelationContext:
    """Test the CorrelationContext functionality"""

    def setup_method(self):
        """Clear context before each test"""
        CorrelationContext.clear()

    def teardown_method(self):
        """Clear context after each test"""
        CorrelationContext.clear()

    def test_create_correlation_id(self):
        """Test correlation ID creation"""
        cid = create_correlation_id()
        assert cid.startswith("corr_")
        assert len(cid) == 21  # corr_ + 16 hex chars

    def test_get_set_correlation_id(self):
        """Test getting and setting correlation ID"""
        # Clear any existing context first
        CorrelationContext.clear()

        # Initially should be None
        assert get_correlation_id() is None

        # Set a correlation ID
        test_id = "test_correlation_123"
        set_correlation_id(test_id)
        assert get_correlation_id() == test_id

        # Clear it
        CorrelationContext.clear()
        assert get_correlation_id() is None

    def test_get_or_create_id(self):
        """Test get_or_create_id functionality"""
        # When no ID exists, should create one
        cid1 = CorrelationContext.get_or_create_id()
        assert cid1.startswith("corr_")

        # When ID exists, should return existing
        cid2 = CorrelationContext.get_or_create_id()
        assert cid1 == cid2

        # When provided ID, should use that
        custom_id = "custom_correlation_456"
        cid3 = CorrelationContext.get_or_create_id(custom_id)
        assert cid3 == custom_id

    def test_extract_from_headers(self):
        """Test extracting correlation ID from headers"""
        headers = {
            "X-Correlation-ID": "test_123",
            "Content-Type": "application/json"
        }
        cid = CorrelationContext.extract_from_headers(headers)
        assert cid == "test_123"

        # Test alternative header names
        headers2 = {"X-Request-ID": "req_456"}
        cid2 = CorrelationContext.extract_from_headers(headers2)
        assert cid2 == "req_456"

        # Test no correlation header
        headers3 = {"Content-Type": "application/json"}
        cid3 = CorrelationContext.extract_from_headers(headers3)
        assert cid3 is None

    def test_inject_into_headers(self):
        """Test injecting correlation ID into headers"""
        headers = {"Content-Type": "application/json"}

        # Set a correlation ID in context
        set_correlation_id("test_789")
        updated = CorrelationContext.inject_into_headers(headers)

        assert updated["X-Correlation-ID"] == "test_789"
        assert updated["Content-Type"] == "application/json"


class TestCorrelationDecorator:
    """Test the @with_correlation_id decorator"""

    @pytest.mark.asyncio
    async def test_async_decorator(self):
        """Test decorator with async function"""
        call_count = 0
        captured_id = None

        @with_correlation_id
        async def test_handler(correlation_id=None):
            nonlocal call_count, captured_id
            call_count += 1
            captured_id = correlation_id
            return {"status": "ok", "correlation_id": correlation_id}

        # Clear any existing correlation ID
        CorrelationContext.clear()

        # Call without correlation ID - should generate one
        result = await test_handler()
        assert call_count == 1
        assert captured_id is not None
        assert captured_id.startswith("corr_")
        assert result["correlation_id"] == captured_id

        # Call with correlation ID in kwargs
        result2 = await test_handler(correlation_id="explicit_123")
        assert result2["correlation_id"] == "explicit_123"

    def test_sync_decorator(self):
        """Test decorator with sync function"""
        @with_correlation_id
        def test_handler(data, correlation_id=None):
            return {"data": data, "correlation_id": correlation_id}

        CorrelationContext.clear()

        result = test_handler("test_data")
        assert result["correlation_id"] is not None
        assert result["correlation_id"].startswith("corr_")
        assert result["data"] == "test_data"


class TestServiceCorrelation:
    """Test correlation ID propagation in Cliffracer services"""

    @pytest.mark.asyncio
    async def test_rpc_correlation_propagation(self):
        """Test correlation ID propagation through RPC calls"""
        config = ServiceConfig(name="test_service")

        class TestService(CliffracerService):
            def __init__(self):
                super().__init__(config)
                self.received_correlation_ids = []

            @rpc
            async def test_method(self, data: str, correlation_id: str = None):
                self.received_correlation_ids.append(correlation_id)
                return {"echo": data, "correlation_id": correlation_id}

        service = TestService()

        # Mock NATS connection
        mock_nc = AsyncMock()
        service.nc = mock_nc

        # Create a mock message
        mock_msg = MagicMock()
        mock_msg.subject = "test_service.rpc.test_method"
        mock_msg.data = json.dumps({
            "data": "hello",
            "correlation_id": "test_corr_123"
        }).encode()
        mock_msg.respond = AsyncMock()

        # Handle the RPC request
        await service._handle_rpc_request(mock_msg)

        # Verify correlation ID was received
        assert len(service.received_correlation_ids) == 1
        assert service.received_correlation_ids[0] == "test_corr_123"

        # Verify response includes correlation ID
        mock_msg.respond.assert_called_once()
        response_data = json.loads(mock_msg.respond.call_args[0][0].decode())
        assert response_data["correlation_id"] == "test_corr_123"
        assert response_data["result"]["correlation_id"] == "test_corr_123"

    @pytest.mark.asyncio
    async def test_rpc_correlation_generation(self):
        """Test correlation ID generation when not provided"""
        config = ServiceConfig(name="test_service")

        class TestService(CliffracerService):
            def __init__(self):
                super().__init__(config)

            @rpc
            async def test_method(self, correlation_id: str = None):
                return {"correlation_id": correlation_id}

        service = TestService()

        # Mock NATS connection
        mock_nc = AsyncMock()
        service.nc = mock_nc

        # Create a mock message without correlation ID
        mock_msg = MagicMock()
        mock_msg.subject = "test_service.rpc.test_method"
        mock_msg.data = json.dumps({}).encode()
        mock_msg.respond = AsyncMock()
        mock_msg.headers = None  # Explicitly set headers to None

        # Handle the RPC request
        await service._handle_rpc_request(mock_msg)

        # Verify correlation ID was generated
        response_data = json.loads(mock_msg.respond.call_args[0][0].decode())
        assert response_data["correlation_id"] is not None
        assert response_data["correlation_id"].startswith("corr_")
        assert response_data["result"]["correlation_id"] == response_data["correlation_id"]

    @pytest.mark.asyncio
    async def test_event_correlation_propagation(self):
        """Test correlation ID propagation through events"""
        config = ServiceConfig(name="test_service")

        captured_correlation_id = None

        class TestService(CliffracerService):
            def __init__(self):
                super().__init__(config)
                self._event_handlers["test.event"] = self.handle_test_event

            async def handle_test_event(self, subject: str, correlation_id: str = None, **data):
                nonlocal captured_correlation_id
                captured_correlation_id = correlation_id

        service = TestService()

        # Create a mock event message
        mock_msg = MagicMock()
        mock_msg.subject = "test.event"
        mock_msg.data = json.dumps({
            "event_data": "test",
            "correlation_id": "event_corr_456"
        }).encode()

        # Handle the event
        await service._handle_event(mock_msg)

        # Verify correlation ID was propagated
        assert captured_correlation_id == "event_corr_456"

    @pytest.mark.asyncio
    async def test_client_call_correlation_propagation(self):
        """Test correlation ID propagation in client RPC calls"""
        config = ServiceConfig(name="test_service")
        service = CliffracerService(config)

        # Mock NATS connection
        mock_nc = AsyncMock()
        mock_response = MagicMock()
        mock_response.data = json.dumps({"result": "success"}).encode()
        mock_nc.request = AsyncMock(return_value=mock_response)
        service.nc = mock_nc

        # Set correlation ID in context
        set_correlation_id("client_corr_789")

        # Make RPC call
        await service.call_rpc("other_service", "method", param="value")

        # Verify correlation ID was included in request
        mock_nc.request.assert_called_once()
        call_args = mock_nc.request.call_args
        request_data = json.loads(call_args[0][1].decode())
        assert request_data["correlation_id"] == "client_corr_789"

        # Clear context
        CorrelationContext.clear()


class TestHTTPCorrelation:
    """Test correlation ID handling in HTTP requests"""

    def test_http_correlation_middleware(self):
        """Test HTTP middleware extracts and propagates correlation ID"""
        config = ServiceConfig(name="http_test_service")

        captured_correlation_ids = []

        class TestHTTPService(HTTPNATSService):
            def __init__(self):
                super().__init__(config, host="127.0.0.1", port=8001)

                @self.app.get("/test")
                async def test_endpoint(correlation_id: str = None):
                    captured_correlation_ids.append(correlation_id)
                    return {"correlation_id": correlation_id}

        service = TestHTTPService()
        client = TestClient(service.app)

        # Test with correlation ID in header
        response = client.get("/test", headers={"X-Correlation-ID": "http_corr_123"})
        assert response.status_code == 200
        assert response.headers["X-Correlation-ID"] == "http_corr_123"

        # Test without correlation ID - should generate one
        response2 = client.get("/test")
        assert response2.status_code == 200
        assert "X-Correlation-ID" in response2.headers
        assert response2.headers["X-Correlation-ID"].startswith("corr_")

    def test_correlation_dependency(self):
        """Test FastAPI dependency injection for correlation ID"""
        from fastapi import Depends, FastAPI

        app = FastAPI()

        captured_id = None

        @app.get("/test")
        async def test_endpoint(correlation_id: str = Depends(correlation_id_dependency)):
            nonlocal captured_id
            captured_id = correlation_id
            return {"correlation_id": correlation_id}

        # Add correlation middleware
        from cliffracer.middleware.correlation import CorrelationMiddleware
        app.add_middleware(CorrelationMiddleware)

        client = TestClient(app)

        # Test request
        response = client.get("/test", headers={"X-Correlation-ID": "dep_test_456"})
        assert response.status_code == 200
        assert captured_id == "dep_test_456"


@pytest.mark.asyncio
async def test_correlation_logging():
    """Test that correlation IDs appear in logs"""
    import os
    import tempfile

    from loguru import logger

    from cliffracer.logging.correlation_logging import setup_correlation_logging

    with tempfile.TemporaryDirectory() as tmpdir:
        # Setup logging with temp directory
        old_cwd = os.getcwd()
        os.chdir(tmpdir)
        os.makedirs("logs", exist_ok=True)

        try:
            setup_correlation_logging("test_service", "DEBUG")

            # Set correlation ID
            set_correlation_id("log_test_789")

            # Log a message
            logger.info("Test log message")

            # Read log file
            with open("logs/test_service.log") as f:
                log_content = f.read()

            # Verify correlation ID is in log
            assert "log_test_789" in log_content
            assert "Test log message" in log_content

        finally:
            os.chdir(old_cwd)
            CorrelationContext.clear()


@pytest.mark.asyncio
async def test_end_to_end_correlation():
    """Test correlation ID flows through entire service chain"""

    # Create two services that communicate
    config1 = ServiceConfig(name="service1")
    config2 = ServiceConfig(name="service2")

    class Service1(CliffracerService):
        def __init__(self):
            super().__init__(config1)

        @rpc
        async def call_service2(self, correlation_id: str = None):
            # Call service2
            result = await self.call_rpc("service2", "process", data="test")
            return {
                "service1_correlation_id": correlation_id,
                "service2_result": result
            }

    class Service2(CliffracerService):
        def __init__(self):
            super().__init__(config2)

        @rpc
        async def process(self, data: str, correlation_id: str = None):
            return {
                "processed": data,
                "service2_correlation_id": correlation_id
            }

    service1 = Service1()
    service2 = Service2()

    # Mock NATS connections
    mock_nc1 = AsyncMock()
    mock_nc2 = AsyncMock()

    # Setup service2 to respond to service1's call
    async def mock_request(subject, data, timeout):
        if subject == "service2.rpc.process":
            # Simulate service2 handling the request
            request_data = json.loads(data.decode())
            response = {
                "result": {
                    "processed": request_data["data"],
                    "service2_correlation_id": request_data["correlation_id"]
                },
                "correlation_id": request_data["correlation_id"]
            }
            mock_response = MagicMock()
            mock_response.data = json.dumps(response).encode()
            return mock_response

    mock_nc1.request = mock_request
    service1.nc = mock_nc1
    service2.nc = mock_nc2

    # Simulate initial RPC call to service1
    initial_correlation_id = "e2e_test_999"
    mock_msg = MagicMock()
    mock_msg.subject = "service1.rpc.call_service2"
    mock_msg.data = json.dumps({"correlation_id": initial_correlation_id}).encode()
    mock_msg.respond = AsyncMock()

    # Handle the request
    await service1._handle_rpc_request(mock_msg)

    # Verify correlation ID propagated through both services
    response_data = json.loads(mock_msg.respond.call_args[0][0].decode())
    assert response_data["correlation_id"] == initial_correlation_id
    assert response_data["result"]["service1_correlation_id"] == initial_correlation_id
    assert response_data["result"]["service2_result"]["service2_correlation_id"] == initial_correlation_id


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
