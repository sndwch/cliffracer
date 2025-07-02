"""
Tests for Saga pattern implementation
"""

import asyncio
import pytest
from unittest.mock import AsyncMock, MagicMock

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.patterns.saga import (
    SagaCoordinator, SagaParticipant, SagaStep, 
    SagaState, StepState, ChoreographySaga
)


@pytest.fixture
def mock_service():
    """Create a mock Cliffracer service"""
    service = MagicMock(spec=CliffracerService)
    service.rpc_call = AsyncMock()
    service.publish = AsyncMock()
    service._rpc_handlers = {}
    
    # Add RPC decorator
    def rpc_decorator(func):
        service._rpc_handlers[func.__name__] = func
        return func
    
    service.rpc = property(lambda self: rpc_decorator)
    
    return service


@pytest.fixture
def saga_coordinator(mock_service):
    """Create a saga coordinator"""
    return SagaCoordinator(mock_service, persistence_enabled=False)


class TestSagaCoordinator:
    """Test saga coordinator functionality"""
    
    def test_define_saga(self, saga_coordinator):
        """Test defining a saga"""
        steps = [
            SagaStep("step1", "service1", "action1", "compensate1"),
            SagaStep("step2", "service2", "action2", "compensate2")
        ]
        
        saga_coordinator.define_saga("test_saga", steps)
        
        assert "test_saga" in saga_coordinator.saga_definitions
        assert len(saga_coordinator.saga_definitions["test_saga"]) == 2
    
    @pytest.mark.asyncio
    async def test_start_saga(self, saga_coordinator, mock_service):
        """Test starting a saga"""
        # Define saga
        steps = [
            SagaStep("step1", "service1", "action1", "compensate1")
        ]
        saga_coordinator.define_saga("test_saga", steps)
        
        # Start saga
        result = await saga_coordinator._start_saga("test_saga", {"data": "test"})
        
        assert "saga_id" in result
        assert "correlation_id" in result
        assert result["status"] == "started"
        assert result["saga_id"] in saga_coordinator.active_sagas
    
    @pytest.mark.asyncio
    async def test_execute_step_success(self, saga_coordinator, mock_service):
        """Test successful step execution"""
        # Mock successful RPC response
        mock_service.rpc_call.return_value = {"result": {"status": "ok"}}
        
        # Create context and step
        from cliffracer.patterns.saga import SagaContext
        context = SagaContext(saga_type="test", data={"test": "data"})
        step = SagaStep("test_step", "test_service", "test_action")
        
        # Execute step
        success = await saga_coordinator._execute_step(context, step)
        
        assert success is True
        assert step.state == StepState.COMPLETED
        assert step.result == {"status": "ok"}
        assert mock_service.rpc_call.called
    
    @pytest.mark.asyncio
    async def test_execute_step_failure(self, saga_coordinator, mock_service):
        """Test step execution failure"""
        # Mock failed RPC response
        mock_service.rpc_call.return_value = {"error": "Test error"}
        
        # Create context and step with no retries
        from cliffracer.patterns.saga import SagaContext
        context = SagaContext(saga_type="test", data={"test": "data"})
        step = SagaStep("test_step", "test_service", "test_action", retry_count=1)
        
        # Execute step
        success = await saga_coordinator._execute_step(context, step)
        
        assert success is False
        assert step.state == StepState.FAILED
        assert step.error == "Test error"
    
    @pytest.mark.asyncio
    async def test_compensate_saga(self, saga_coordinator, mock_service):
        """Test saga compensation"""
        # Mock compensation responses
        mock_service.rpc_call.return_value = {"result": {"status": "compensated"}}
        
        # Create context with completed steps
        from cliffracer.patterns.saga import SagaContext
        context = SagaContext(saga_type="test")
        
        # Add completed steps
        step1 = SagaStep("step1", "service1", "action1", "compensate1")
        step1.state = StepState.COMPLETED
        step1.result = {"data": "result1"}
        
        step2 = SagaStep("step2", "service2", "action2", "compensate2")
        step2.state = StepState.COMPLETED
        step2.result = {"data": "result2"}
        
        context.steps = [step1, step2]
        context.current_step = 1
        
        # Compensate saga
        await saga_coordinator._compensate_saga(context)
        
        assert context.state == SagaState.COMPENSATED
        assert mock_service.rpc_call.call_count == 2  # Both compensations called
        assert step1.state == StepState.COMPENSATED
        assert step2.state == StepState.COMPENSATED


class TestSagaParticipant:
    """Test saga participant functionality"""
    
    @pytest.mark.asyncio
    async def test_execute_action(self, mock_service):
        """Test executing saga action"""
        class TestParticipant(SagaParticipant):
            def _register_handlers(self):
                pass
            
            async def handle_test_action(self, data):
                return {"result": "success", "input": data}
        
        participant = TestParticipant(mock_service)
        result = await participant.execute_action("test_action", {"test": "data"})
        
        assert result["result"] == "success"
        assert result["input"]["test"] == "data"
    
    @pytest.mark.asyncio
    async def test_execute_compensation(self, mock_service):
        """Test executing compensation"""
        class TestParticipant(SagaParticipant):
            def _register_handlers(self):
                pass
            
            async def compensate_test_action(self, data):
                return {"compensated": True, "original": data}
        
        participant = TestParticipant(mock_service)
        result = await participant.execute_compensation("test_action", {"test": "data"})
        
        assert result["compensated"] is True
        assert result["original"]["test"] == "data"


class TestChoreographySaga:
    """Test choreography-based saga"""
    
    def test_event_decorator(self, mock_service):
        """Test event decorator registration"""
        saga = ChoreographySaga(mock_service)
        
        @saga.on_event("test.event")
        async def handle_test_event(data):
            return {"handled": True}
        
        assert "test.event" in saga.subscriptions
    
    def test_emits_decorator(self, mock_service):
        """Test emits decorator"""
        saga = ChoreographySaga(mock_service)
        
        @saga.on_event("test.event")
        @saga.emits("success.event", "failure.event")
        async def handle_test_event(data):
            return {"handled": True}
        
        # Check that success/failure events are attached
        handler = mock_service.event.call_args[0][0]
        assert hasattr(handler, "_success_event")
        assert hasattr(handler, "_failure_event")
        assert handler._success_event == "success.event"
        assert handler._failure_event == "failure.event"


@pytest.mark.asyncio
async def test_saga_context_serialization():
    """Test saga context serialization"""
    from cliffracer.patterns.saga import SagaContext
    
    context = SagaContext(
        saga_type="test_saga",
        data={"key": "value"}
    )
    
    # Add a step
    step = SagaStep("test", "service", "action", "compensation")
    context.steps.append(step)
    
    # Serialize
    data = context.to_dict()
    
    assert data["saga_type"] == "test_saga"
    assert data["data"]["key"] == "value"
    assert len(data["steps"]) == 1
    assert data["steps"][0]["name"] == "test"