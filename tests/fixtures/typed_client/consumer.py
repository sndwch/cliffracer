"""Fixture service that communicates with a service using a generated client."""

from typing import Any

from cliffracer import CliffracerService, ServiceConfig, rpc

from .models import Line, Order


class Consumer(CliffracerService):
    """Wrapper for a generated client class. The class is injected at runtime."""

    def __init__(self, client_class: Any, *, service: str, headers: dict[str, str] | None = None):
        super().__init__(ServiceConfig(name="consumer_e2e"))
        self._client_class = client_class
        self._target = service
        self._headers = dict(headers or {})
        self.warehouse: Any = None

    async def on_startup(self) -> None:
        self.warehouse = self._client_class(self.nc, service=self._target, headers=self._headers)

    @rpc
    async def restock(self, sku: str, qty: int = 1) -> str:
        """Restock an item via generated client order creation."""
        receipt = await self.warehouse.create(Order(lines=[Line(sku=sku, qty=qty)]))
        return receipt.order_id
