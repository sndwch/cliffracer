"""Outbound messaging interceptor chains and context factories."""

from __future__ import annotations

from collections.abc import Awaitable, Callable
from typing import Any

from ..extension import WorkerContext
from ..service_config import ServiceConfig
from .pipeline import ExtensionPipeline


class OutboundDispatcher:
    """Constructs outbound worker contexts and executes outbound send hooks.

    Invariants:
    - Prepares WorkerContext with correlation ID and Content-Type headers.
    - Executes before_call and after_call hooks around outbound network operations.
    """

    def __init__(
        self,
        config: ServiceConfig,
        pipeline: ExtensionPipeline,
    ) -> None:
        self.config = config
        self.pipeline = pipeline

    def send_context(
        self, kind: str, subject: str, payload: dict[str, Any], correlation_id: str
    ) -> WorkerContext:
        """Construct an initialized outbound WorkerContext."""
        return self.pipeline.create_send_context(
            kind=kind,
            subject=subject,
            payload=payload,
            correlation_id=correlation_id,
            serialization_format=self.config.serialization_format,
        )

    async def run_send_hooks(self, ctx: WorkerContext, send: Callable[[], Awaitable[Any]]) -> Any:
        """Execute extension outbound hooks around send."""
        return await self.pipeline.run_send_hooks(ctx, send)

    # Compatibility aliases
    _send_context = send_context
    _run_send_hooks = run_send_hooks
