"""In-memory test harness and dispatch runner for service handlers."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock

from cliffracer.core.container import Container, DispatchOutcome
from cliffracer.core.service import CliffracerService
from cliffracer.core.service_config import ServiceConfig
from cliffracer.core.validation import serialize_payload
from cliffracer.testing.messages import MockMessage, TestResponse


class ServiceTestHarness:
    """In-memory service test harness.

    Orchestrates Container initialization, extension lifecycle, and message
    dispatch using mock message envelopes. Never connects to a live NATS broker
    or opens network sockets.
    """

    def __init__(
        self,
        service: CliffracerService | type[CliffracerService],
        *,
        config: ServiceConfig | None = None,
    ) -> None:
        if isinstance(service, type):
            cfg = config or ServiceConfig(name="test_harness_svc", health_port=0)
            self._service = service(cfg)
        else:
            self._service = service

        self._container: Container = self._service.container
        self._mock_nc = AsyncMock()
        self._mock_nc.is_connected = True
        self._mock_nc.is_closed = False
        self._mock_nc.is_draining = False
        if not self._container.nc:
            self._container.nc = self._mock_nc
        self._initialized = False

    @property
    def service(self) -> CliffracerService:
        """The service instance under test."""
        return self._service

    @property
    def container(self) -> Container:
        """The container instance bound to the service."""
        return self._container

    async def __aenter__(self) -> ServiceTestHarness:
        await self.setup()
        return self

    async def __aexit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        await self.teardown()

    async def setup(self) -> None:
        """Initialize extensions, discover handlers, and mark running."""
        if not self._initialized:
            await self._container._setup_extensions()
            self._container.discover_handlers()
            self._container.lifecycle._running = True
            self._initialized = True

    async def teardown(self) -> None:
        """Drain in-flight tasks and stop extensions."""
        if self._initialized:
            self._container.lifecycle._running = False
            if self._container.lifecycle.active_tasks:
                await asyncio.gather(
                    *list(self._container.lifecycle.active_tasks), return_exceptions=True
                )
            await self._container._stop_extensions()
            self._initialized = False

    async def rpc(
        self,
        method: str,
        *,
        payload: Any = None,
        headers: dict[str, str] | None = None,
        format: str = "json",
        **kwargs: Any,
    ) -> TestResponse:
        """Invoke an RPC method handler and return the decoded TestResponse."""
        await self.setup()
        data = payload if payload is not None else kwargs
        raw_bytes, content_type = serialize_payload(data, format=format)
        req_headers = dict(headers or {})
        req_headers["Content-Type"] = content_type

        subject = self._container._with_namespace(f"{self._service.config.name}.rpc.{method}")
        msg = MockMessage(
            subject=subject,
            data=raw_bytes,
            headers=req_headers,
        )
        await self._container.dispatcher.handle_rpc_request(msg)
        return TestResponse.from_mock_message(msg)

    async def emit_event(
        self,
        subject: str,
        data: Any = None,
        *,
        headers: dict[str, str] | None = None,
        format: str = "json",
        pattern: str | None = None,
        **kwargs: Any,
    ) -> DispatchOutcome:
        """Emit an event through the service container dispatch pipeline."""
        await self.setup()
        payload = data if data is not None else kwargs
        raw_bytes, content_type = serialize_payload(payload, format=format)
        req_headers = dict(headers or {})
        req_headers["Content-Type"] = content_type

        msg = MockMessage(subject=subject, data=raw_bytes, headers=req_headers)
        return await self._container.dispatcher.handle_event(
            msg, pattern=pattern, raise_on_error=False
        )

    publish = emit_event

    async def describe(self) -> dict[str, Any]:
        """Invoke the service describe endpoint and return the description dict."""
        await self.setup()
        subject = self._container._with_namespace(f"{self._service.config.name}.describe")
        msg = MockMessage(subject=subject)
        await self._container.dispatcher.handle_describe_request(msg)
        resp = TestResponse.from_mock_message(msg)
        if isinstance(resp.data, dict):
            return resp.data
        return {}
