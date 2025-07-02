"""
Saga Pattern implementation for Cliffracer

Provides distributed transaction management using the Saga pattern
with support for both choreography and orchestration approaches.
"""

import asyncio
import uuid
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from typing import Any

from loguru import logger

from ..core.consolidated_service import CliffracerService
from ..core.correlation import CorrelationContext


class SagaState(Enum):
    """States for a saga execution"""
    PENDING = "pending"
    RUNNING = "running"
    COMPENSATING = "compensating"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATED = "compensated"


class StepState(Enum):
    """States for individual saga steps"""
    PENDING = "pending"
    EXECUTING = "executing"
    COMPLETED = "completed"
    FAILED = "failed"
    COMPENSATING = "compensating"
    COMPENSATED = "compensated"


@dataclass
class SagaStep:
    """Represents a single step in a saga"""
    name: str
    service: str
    action: str
    compensation: str | None = None
    timeout: float = 30.0
    retry_count: int = 3
    retry_delay: float = 1.0

    # Runtime state
    state: StepState = StepState.PENDING
    result: Any | None = None
    error: str | None = None
    attempts: int = 0
    started_at: datetime | None = None
    completed_at: datetime | None = None


@dataclass
class SagaContext:
    """Context for saga execution"""
    saga_id: str = field(default_factory=lambda: str(uuid.uuid4()))
    correlation_id: str = field(default_factory=lambda: CorrelationContext.get_or_create_id())
    saga_type: str = ""
    state: SagaState = SagaState.PENDING
    steps: list[SagaStep] = field(default_factory=list)
    current_step: int = 0
    data: dict[str, Any] = field(default_factory=dict)
    created_at: datetime = field(default_factory=lambda: datetime.now(UTC))
    completed_at: datetime | None = None
    error: str | None = None

    def to_dict(self) -> dict[str, Any]:
        """Convert to dictionary for persistence"""
        return {
            "saga_id": self.saga_id,
            "correlation_id": self.correlation_id,
            "saga_type": self.saga_type,
            "state": self.state.value,
            "current_step": self.current_step,
            "data": self.data,
            "created_at": self.created_at.isoformat(),
            "completed_at": self.completed_at.isoformat() if self.completed_at else None,
            "error": self.error,
            "steps": [
                {
                    "name": step.name,
                    "service": step.service,
                    "action": step.action,
                    "compensation": step.compensation,
                    "state": step.state.value,
                    "result": step.result,
                    "error": step.error,
                    "attempts": step.attempts
                }
                for step in self.steps
            ]
        }


class SagaParticipant(ABC):
    """Base class for saga participants"""

    def __init__(self, service: CliffracerService):
        self.service = service
        self._register_handlers()

    @abstractmethod
    def _register_handlers(self):
        """Register RPC handlers for saga actions"""
        pass

    async def execute_action(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        """Execute a saga action"""
        handler = getattr(self, f"handle_{action}", None)
        if not handler:
            raise ValueError(f"Unknown action: {action}")

        return await handler(data)

    async def execute_compensation(self, action: str, data: dict[str, Any]) -> dict[str, Any]:
        """Execute a compensating action"""
        handler = getattr(self, f"compensate_{action}", None)
        if not handler:
            raise ValueError(f"No compensation for action: {action}")

        return await handler(data)


class SagaCoordinator:
    """Orchestrates saga execution"""

    def __init__(self, service: CliffracerService, persistence_enabled: bool = True):
        self.service = service
        self.persistence_enabled = persistence_enabled
        self.active_sagas: dict[str, SagaContext] = {}
        self.saga_definitions: dict[str, list[SagaStep]] = {}

        # Register RPC handlers
        self._register_handlers()

    def _register_handlers(self):
        """Register coordinator RPC handlers"""
        @self.service.rpc
        async def start_saga(saga_type: str, data: dict[str, Any]) -> dict[str, Any]:
            """Start a new saga"""
            return await self._start_saga(saga_type, data)

        @self.service.rpc
        async def get_saga_status(saga_id: str) -> dict[str, Any]:
            """Get saga status"""
            if saga_id not in self.active_sagas:
                return {"error": "Saga not found"}

            return self.active_sagas[saga_id].to_dict()

    def define_saga(self, saga_type: str, steps: list[SagaStep]):
        """Define a saga type with its steps"""
        self.saga_definitions[saga_type] = steps
        logger.info(f"Defined saga type: {saga_type} with {len(steps)} steps")

    async def _start_saga(self, saga_type: str, data: dict[str, Any]) -> dict[str, Any]:
        """Start a new saga execution"""
        if saga_type not in self.saga_definitions:
            return {"error": f"Unknown saga type: {saga_type}"}

        # Create saga context
        context = SagaContext(
            saga_type=saga_type,
            steps=[SagaStep(**step.__dict__) for step in self.saga_definitions[saga_type]],
            data=data
        )

        self.active_sagas[context.saga_id] = context

        # Start execution
        asyncio.create_task(self._execute_saga(context))

        return {
            "saga_id": context.saga_id,
            "correlation_id": context.correlation_id,
            "status": "started"
        }

    async def _execute_saga(self, context: SagaContext):
        """Execute the saga"""
        try:
            context.state = SagaState.RUNNING
            logger.info(f"Starting saga execution: {context.saga_id}")

            # Execute steps in order
            for i, step in enumerate(context.steps):
                context.current_step = i

                # Execute step
                success = await self._execute_step(context, step)

                if not success:
                    # Step failed, start compensation
                    logger.error(f"Step {step.name} failed, starting compensation")
                    await self._compensate_saga(context)
                    return

            # All steps completed successfully
            context.state = SagaState.COMPLETED
            context.completed_at = datetime.now(UTC)
            logger.info(f"Saga completed successfully: {context.saga_id}")

            # Emit completion event
            await self.service.publish(
                f"saga.{context.saga_type}.completed",
                context.to_dict()
            )

        except Exception as e:
            logger.error(f"Saga execution error: {e}")
            context.state = SagaState.FAILED
            context.error = str(e)
            await self._compensate_saga(context)

    async def _execute_step(self, context: SagaContext, step: SagaStep) -> bool:
        """Execute a single saga step"""
        step.state = StepState.EXECUTING
        step.started_at = datetime.now(UTC)

        for attempt in range(step.retry_count):
            try:
                step.attempts = attempt + 1

                # Call the service action via RPC
                response = await self.service.rpc_call(
                    f"{step.service}.{step.action}",
                    {
                        "saga_id": context.saga_id,
                        "correlation_id": context.correlation_id,
                        "step": step.name,
                        "data": context.data
                    },
                    timeout=step.timeout
                )

                # Check response
                if "error" in response:
                    raise Exception(response["error"])

                # Step succeeded
                step.state = StepState.COMPLETED
                step.completed_at = datetime.now(UTC)
                step.result = response.get("result", {})

                # Update saga data with step result
                context.data[f"{step.name}_result"] = step.result

                logger.info(f"Step completed: {step.name}")
                return True

            except Exception as e:
                logger.error(f"Step {step.name} attempt {attempt + 1} failed: {e}")
                step.error = str(e)

                if attempt < step.retry_count - 1:
                    await asyncio.sleep(step.retry_delay)
                else:
                    step.state = StepState.FAILED
                    return False

        return False

    async def _compensate_saga(self, context: SagaContext):
        """Compensate a failed saga"""
        context.state = SagaState.COMPENSATING
        logger.info(f"Starting saga compensation: {context.saga_id}")

        # Compensate in reverse order, only for completed steps
        for i in range(context.current_step, -1, -1):
            step = context.steps[i]

            if step.state == StepState.COMPLETED and step.compensation:
                await self._compensate_step(context, step)

        context.state = SagaState.COMPENSATED
        context.completed_at = datetime.now(UTC)

        # Emit compensation event
        await self.service.publish(
            f"saga.{context.saga_type}.compensated",
            context.to_dict()
        )

    async def _compensate_step(self, context: SagaContext, step: SagaStep):
        """Compensate a single step"""
        step.state = StepState.COMPENSATING

        try:
            # Call the compensation action
            await self.service.rpc_call(
                f"{step.service}.{step.compensation}",
                {
                    "saga_id": context.saga_id,
                    "correlation_id": context.correlation_id,
                    "step": step.name,
                    "data": context.data,
                    "original_result": step.result
                },
                timeout=step.timeout
            )

            step.state = StepState.COMPENSATED
            logger.info(f"Step compensated: {step.name}")

        except Exception as e:
            logger.error(f"Compensation failed for step {step.name}: {e}")
            # Log but continue with other compensations


class ChoreographySaga:
    """Choreography-based saga implementation"""

    def __init__(self, service: CliffracerService):
        self.service = service
        self.saga_id = str(uuid.uuid4())
        self.subscriptions: set[str] = set()

    def on_event(self, event_pattern: str):
        """Decorator to handle saga events"""
        def decorator(func):
            # Subscribe to event
            self.subscriptions.add(event_pattern)

            @self.service.event(event_pattern)
            async def wrapper(data: dict):
                # Add saga context
                data["saga_id"] = data.get("saga_id", self.saga_id)
                data["correlation_id"] = CorrelationContext.get_or_create_id()

                # Execute handler
                try:
                    result = await func(data)

                    # Emit success event if specified
                    if hasattr(func, "_success_event"):
                        await self.service.publish(
                            func._success_event,
                            {
                                "saga_id": data["saga_id"],
                                "correlation_id": data["correlation_id"],
                                "result": result
                            }
                        )

                    return result

                except Exception as e:
                    logger.error(f"Saga step failed: {e}")

                    # Emit failure event if specified
                    if hasattr(func, "_failure_event"):
                        await self.service.publish(
                            func._failure_event,
                            {
                                "saga_id": data["saga_id"],
                                "correlation_id": data["correlation_id"],
                                "error": str(e)
                            }
                        )

                    raise

            return wrapper
        return decorator

    def emits(self, success_event: str, failure_event: str | None = None):
        """Specify events to emit on success/failure"""
        def decorator(func):
            func._success_event = success_event
            if failure_event:
                func._failure_event = failure_event
            return func
        return decorator
