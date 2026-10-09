"""A generated client keeps a live handler's semantic refusal distinct from a server fault."""

from __future__ import annotations

from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceClient, ServiceConfig, rpc
from cliffracer.core.exceptions import RpcServerError, RpcValidationError
from cliffracer.generate_client import emit
from cliffracer.introspect import describe

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

DETAILS = [
    {
        "type": "unknown_parameter",
        "loc": ["parameters", "threshold"],
        "msg": "threshold is not supported",
        "input": "unexpected",
    }
]


class SemanticService(CliffracerService):
    @rpc
    async def evaluate(self, parameters: dict[str, str]) -> int:
        if "threshold" in parameters:
            raise RpcValidationError(DETAILS, message="unsupported parameter")
        return len(parameters)

    @rpc
    async def crashes(self) -> int:
        raise RuntimeError("implementation broke")


def _client(nc: Any, service: SemanticService) -> ServiceClient:
    namespace: dict[str, Any] = {}
    description = describe(
        SemanticService,
        service=service.config.name,
        version=service.config.version,
    )
    exec(compile(emit(description), "<generated>", "exec"), namespace)  # noqa: S102
    cls = next(
        value
        for value in namespace.values()
        if isinstance(value, type)
        and issubclass(value, ServiceClient)
        and value is not ServiceClient
    )
    return cls(nc=nc, service=service.config.name)


async def test_a_generated_client_reads_semantic_invalidity_without_conflating_failures(
    nats_connection,
):
    service = SemanticService(
        ServiceConfig(
            name="semantic_refusal_live",
            version="1",
            nats_url=nats_connection.connected_url.geturl(),
            health_listener=False,
        )
    )
    await service.start()
    client = _client(nats_connection, service)
    try:
        assert await client.evaluate(parameters={}) == 0
        with pytest.raises(RpcValidationError) as refused:
            await client.evaluate(parameters={"threshold": "unexpected"})
        assert refused.value.details == DETAILS
        with pytest.raises(RpcServerError):
            await client.crashes()
    finally:
        await service.stop()
