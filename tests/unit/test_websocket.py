"""
Unit tests for WebSocket functionality
"""

import json
from unittest.mock import AsyncMock, MagicMock, Mock

import pytest
from fastapi import WebSocket

from cliffracer import ServiceConfig, WebSocketNATSService, websocket_handler


class TestWebSocketService:
    """Test WebSocket service functionality"""

    class TestWSService(WebSocketNATSService):
        def __init__(self, config):
            super().__init__(config, port=8003)
            self.messages_received = []

        @websocket_handler("/test")
        async def test_handler(self, websocket):
            """Test WebSocket handler"""
            await websocket.send_json({"type": "connected"})
            async for message in websocket.iter_json():
                self.messages_received.append(message)
                await websocket.send_json({"echo": message})

    @pytest.fixture
    def service_config(self):
        return ServiceConfig(name="test_ws_service")

    @pytest.fixture
    def service(self, service_config):
        return self.TestWSService(service_config)

    def test_websocket_service_initialization(self, service):
        """Test WebSocket service initializes correctly"""
        assert hasattr(service, "_active_connections")
        assert service._active_connections == set()
        assert hasattr(service, "register_websocket_handler")
        assert hasattr(service, "broadcast_to_websockets")

    def test_websocket_handler_decorator(self, service):
        """Test that websocket_handler decorator works"""
        # Check that the handler was registered
        assert hasattr(service.test_handler, "_cliffracer_websocket")
        assert service.test_handler._cliffracer_websocket == "/test"

    @pytest.mark.asyncio
    async def test_websocket_connection_management(self, service):
        """Test WebSocket connection tracking"""
        # Create mock WebSocket
        mock_ws = Mock(spec=WebSocket)
        mock_ws.accept = AsyncMock()
        mock_ws.send_json = AsyncMock()
        mock_ws.receive_json = AsyncMock(side_effect=Exception("Connection closed"))

        # Test connection management
        service._active_connections.add(mock_ws)
        assert len(service._active_connections) == 1

        # Test connection removal
        service._active_connections.discard(mock_ws)
        assert len(service._active_connections) == 0

    @pytest.mark.asyncio
    async def test_broadcast_to_websockets(self, service):
        """Test broadcasting to all WebSocket connections"""
        # Create mock WebSockets
        mock_ws1 = Mock(spec=WebSocket)
        mock_ws1.send_text = AsyncMock()
        mock_ws2 = Mock(spec=WebSocket)
        mock_ws2.send_text = AsyncMock()
        mock_ws3 = Mock(spec=WebSocket)
        mock_ws3.send_text = AsyncMock(side_effect=Exception("Disconnected"))

        # Add connections
        service._active_connections = {mock_ws1, mock_ws2, mock_ws3}

        # Broadcast message
        test_data = {"type": "test", "message": "Hello WebSocket!"}
        await service.broadcast_to_websockets(test_data)

        # Verify broadcasts
        expected_data = json.dumps(test_data)
        mock_ws1.send_text.assert_called_once_with(expected_data)
        mock_ws2.send_text.assert_called_once_with(expected_data)
        mock_ws3.send_text.assert_called_once_with(expected_data)

        # Verify disconnected client was removed
        assert mock_ws3 not in service._active_connections
        assert len(service._active_connections) == 2

    @pytest.mark.asyncio
    @pytest.mark.xfail(reason="Automatic broadcast relay not yet implemented")
    async def test_websocket_auto_relay_broadcast(self, service):
        """Test that BroadcastMessages are automatically relayed to WebSocket clients"""
        # Mock WebSocket connections
        mock_ws = Mock(spec=WebSocket)
        mock_ws.send_text = AsyncMock()
        service._active_connections = {mock_ws}

        # Create a mock broadcast message
        from cliffracer import BroadcastMessage

        class TestBroadcast(BroadcastMessage):
            test_field: str = "test_value"

        broadcast_msg = TestBroadcast(source_service="test_service")

        # Simulate receiving a broadcast message
        # The WebSocketNATSService should have a listener that relays broadcasts
        mock_msg = MagicMock()
        # Use model_dump with mode='json' to handle datetime serialization
        mock_msg.data = json.dumps(broadcast_msg.model_dump(mode="json")).encode()
        mock_msg.subject = "broadcast.testbroadcast"

        # Find the broadcast relay handler
        relay_handler = None
        for attr_name in dir(service):
            attr = getattr(service, attr_name)
            if (
                hasattr(attr, "_is_event_handler")
                and hasattr(attr, "_event_pattern")
                and attr._event_pattern == "broadcast.*"
            ):
                relay_handler = attr
                break

        assert relay_handler is not None, "WebSocket service should have broadcast relay handler"

        # Call the relay handler - it's a bound method, so just pass the message
        await relay_handler(broadcast_msg)

        # Verify the broadcast was relayed to WebSocket
        mock_ws.send_text.assert_called_once()
        call_args_str = mock_ws.send_text.call_args[0][0]
        call_args = json.loads(call_args_str)
        assert call_args["type"] == "broadcast"
        assert "data" in call_args

    def test_websocket_handler_registration(self, service):
        """Test that WebSocket handlers are properly registered"""
        # Verify the test handler is registered
        assert hasattr(service.test_handler, "_cliffracer_websocket")
        assert service.test_handler._cliffracer_websocket == "/test"

        # Check that we can add more handlers
        @websocket_handler("/another")
        async def another_handler(self, websocket):
            pass

        # Bind to service
        bound_handler = another_handler.__get__(service, type(service))
        assert hasattr(bound_handler, "_cliffracer_websocket")
        assert bound_handler._cliffracer_websocket == "/another"

    @pytest.mark.asyncio
    async def test_websocket_error_handling(self, service):
        """Test WebSocket error handling during broadcast"""
        # Create mix of working and failing WebSockets
        mock_ws_good = Mock(spec=WebSocket)
        mock_ws_good.send_text = AsyncMock()

        mock_ws_bad1 = Mock(spec=WebSocket)
        mock_ws_bad1.send_text = AsyncMock(side_effect=RuntimeError("Connection lost"))

        mock_ws_bad2 = Mock(spec=WebSocket)
        mock_ws_bad2.send_text = AsyncMock(side_effect=ConnectionError("Broken pipe"))

        mock_ws_good2 = Mock(spec=WebSocket)
        mock_ws_good2.send_text = AsyncMock()

        # Add all connections
        service._active_connections = {mock_ws_good, mock_ws_bad1, mock_ws_bad2, mock_ws_good2}

        # Broadcast should handle errors gracefully
        await service.broadcast_to_websockets({"test": "message"})

        # Good connections should receive the message
        mock_ws_good.send_text.assert_called_once()
        mock_ws_good2.send_text.assert_called_once()

        # Bad connections should be removed
        assert mock_ws_bad1 not in service._active_connections
        assert mock_ws_bad2 not in service._active_connections
        assert len(service._active_connections) == 2
