"""
Unit tests for base service functionality
"""

import json
from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener, rpc

pytestmark = pytest.mark.unit


class TestNatsService:
    """Test base NatsService class"""

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_service")

    @pytest.fixture
    def service(self, service_config):
        return CliffracerService(service_config)

    def test_service_initialization(self, service, service_config):
        """Test service initialization"""
        assert service.config == service_config
        assert service.nc is None
        assert service.js is None
        assert service.container._subscriptions == set()
        assert service._running is False
        assert service.container.registry.rpc_handlers == {}
        assert service.container.registry.event_handlers == {}

    @pytest.mark.parametrize(
        ("pattern", "subject"),
        [
            ("test.subject", "test.subject"),
            ("test.*", "test.anything"),
            ("test.*", "test.anything.else"),
            ("test.>", "test.anything.else"),
            ("test.subject", "other.subject"),
            ("users.*.created", "users.42.created"),
            ("users.*.created", "users.42.deleted"),
        ],
    )
    def test_the_container_delegates_subject_matching(self, service, pattern, subject):
        """The container's `_subject_matches` is a forward to `cliffracer.core.subjects`, whose
        behaviour `tests/unit/test_subjects.py` covers; this reads that the forward passes the
        pattern and the subject in the right order and returns the answer unchanged."""
        from cliffracer.core.subjects import subject_matches

        assert service.container._subject_matches(pattern, subject) == subject_matches(
            pattern, subject
        )

    def test_CONTROL_the_delegation_cases_are_not_all_one_answer(self, service):
        """Agreeing on a single answer for every case would not tell a forward from a constant."""
        answers = {
            service.container._subject_matches("test.*", "test.anything"),
            service.container._subject_matches("test.*", "other.anything"),
        }

        assert answers == {True, False}

    @pytest.mark.asyncio
    async def test_connection_callbacks(self, service):
        """Test NATS connection callbacks"""
        service.config.on_error = lambda e: setattr(service, "_test_error", e)
        service.config.on_disconnect = lambda: setattr(service, "_test_disconnect", True)
        service.config.on_connect = lambda: setattr(service, "_test_connect", True)

        test_err = Exception("test error")
        await service.container.connection._error_callback(test_err)
        assert getattr(service, "_test_error", None) is test_err

        await service.container.connection._disconnected_callback()
        assert getattr(service, "_test_disconnect", False) is True

        await service.container.connection._reconnected_callback()
        assert getattr(service, "_test_connect", False) is True

        # The service is not running, so a closed connection is only recorded: the early-return
        # branch, whose one effect is this line. (The stop-on-closed branch is exercised, with a
        # running service, in test_scale_empirics_adversarial.py.)
        lines: list[str] = []
        sink = logger.add(lambda m: lines.append(m.record["message"]), level="INFO")
        try:
            await service.container.connection._closed_callback()
        finally:
            logger.remove(sink)
        assert any("connection closed" in line for line in lines), lines
        assert not any("will not be retried" in line for line in lines), lines


class TestExtendedService:
    """Test ExtendedService functionality"""

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_extended_service")

    @pytest.fixture
    def service(self, service_config):
        return CliffracerService(service_config)

    def test_extended_service_initialization(self, service, service_config):
        """Test extended service initialization"""
        assert service.config == service_config
        assert hasattr(service.container.registry, "rpc_specs")
        assert isinstance(service.container.registry.rpc_specs, dict)

    @pytest.mark.asyncio
    async def test_call_rpc_returns_the_result_the_peer_replied_with(self, service):
        """`call_rpc` on the base service sends one request and returns the reply's `result`.

        (The subject-matching and `rpc_specs` checks this test used to carry duplicate
        `test_subject_matches` and `test_extended_service_initialization`.)
        """
        service.nc = AsyncMock()
        mock_response = AsyncMock()
        mock_response.data = json.dumps({"result": "test"}).encode()
        service.nc.request = AsyncMock(return_value=mock_response)

        result = await service.call_rpc("peer", "echo", text="hi")

        assert result == "test"
        assert service.nc.request.await_count == 1
        assert service.nc.request.await_args.args[0].endswith("peer.rpc.echo")


class TestServiceWithDecorators:
    """Test service with decorated methods"""

    class ServiceBaseSvc(CliffracerService):
        def __init__(self, config):
            super().__init__(config)
            self.call_log = []

        @rpc
        async def test_rpc_method(self, param1: str, param2: int = 0) -> dict[str, str]:
            self.call_log.append(f"rpc: {param1}, {param2}")
            return {"result": f"{param1}_{param2}"}

        @listener("test.events.*", fanout=True)
        async def test_event_handler(
            self,
            subject: str,
            user_id: str | None = None,
            event_data: str | None = None,
        ):
            kw = {}
            if user_id is not None:
                kw["user_id"] = user_id
            if event_data is not None:
                kw["event_data"] = event_data
            self.call_log.append(f"event: {subject}, {kw}")

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_decorated_service")

    @pytest.fixture
    def service(self, service_config):
        svc = self.ServiceBaseSvc(service_config)
        svc._discover_handlers()
        return svc

    def test_decorated_methods_registration(self, service):
        """Test that decorated methods are properly registered"""
        # Check RPC method registration
        assert "test_rpc_method" in service.container.registry.rpc_handlers
        # Compare the function objects, not bound methods
        assert (
            service.container.registry.rpc_handlers["test_rpc_method"].__name__ == "test_rpc_method"
        )
        assert hasattr(
            service.container.registry.rpc_handlers["test_rpc_method"], "_cliffracer_rpc"
        )

        # Check event handler registration
        assert "test.events.*" in service.container.registry.event_handlers
        assert (
            service.container.registry.event_handlers["test.events.*"].__name__
            == "test_event_handler"
        )
        assert hasattr(
            service.container.registry.event_handlers["test.events.*"], "_cliffracer_events"
        )

    @pytest.mark.asyncio
    async def test_rpc_decorator_leaves_the_method_callable(self, service):
        """`@rpc` only tags the method: calling it directly runs plain Python.

        This reads that the tag does not wrap or replace the function. Dispatch through the
        framework is `test_rpc_request_handling`.
        """
        # Call the RPC method directly
        result = await service.test_rpc_method("hello", 42)

        assert result == {"result": "hello_42"}
        assert "rpc: hello, 42" in service.call_log

    @pytest.mark.asyncio
    async def test_listener_decorator_leaves_the_method_callable(self, service):
        """`@listener` only tags the method: calling it directly runs plain Python.

        Dispatch through the framework is `test_event_handling`.
        """
        # Call the event handler directly
        await service.test_event_handler("test.events.user_created", user_id="123")

        expected_log = "event: test.events.user_created, {'user_id': '123'}"
        assert expected_log in service.call_log

    @pytest.mark.asyncio
    async def test_rpc_request_handling(self, service, test_helper):
        """Test RPC request handling via message"""
        # Create a mock message
        message = test_helper.create_mock_message(
            subject="test_decorated_service.rpc.test_rpc_method",
            data={"param1": "test", "param2": 123},
        )

        # Handle the RPC request
        await service.container._handle_rpc_request(message)

        # Check that response was sent
        assert message.responded_data is not None

        # Parse the response
        response_data = json.loads(message.responded_data.decode())
        assert "result" in response_data
        assert response_data["result"] == {"result": "test_123"}

    @pytest.mark.asyncio
    async def test_event_handling(self, service, test_helper):
        """Test event handling via message"""
        # Create a mock message
        message = test_helper.create_mock_message(
            subject="test.events.something", data={"event_data": "test"}
        )

        # Handle the event
        await service.container._handle_event(message)

        # Check that event was handled
        expected_log = "event: test.events.something, {'event_data': 'test'}"
        assert expected_log in service.call_log

    @pytest.mark.asyncio
    async def test_unknown_rpc_method(self, service, test_helper):
        """Test handling of unknown RPC method"""
        # Create a mock message for unknown method
        message = test_helper.create_mock_message(
            subject="test_decorated_service.rpc.unknown_method", data={}
        )

        # Handle the RPC request
        await service.container._handle_rpc_request(message)

        # Check that error response was sent
        assert message.responded_data is not None

        # Parse the response
        response_data = json.loads(message.responded_data.decode())
        assert "error" in response_data
        assert "Unknown method" in response_data["error"]
