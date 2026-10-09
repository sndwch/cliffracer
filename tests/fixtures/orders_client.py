"""Business models and service used by generated client contract checks."""

from pydantic import BaseModel, Field

from cliffracer import CliffracerService, rpc


class OrderRequest(BaseModel):
    sku: str
    quantity: int = Field(gt=0)


class OrderReceipt(BaseModel):
    order_id: str
    quantity: int


class Orders(CliffracerService):
    @rpc
    async def create(self, customer: str, request: OrderRequest) -> OrderReceipt:
        return OrderReceipt(order_id=f"{customer}:{request.sku}", quantity=request.quantity)

    @rpc
    async def find(self, order_id: str) -> OrderReceipt | None:
        if order_id == "missing":
            return None
        return OrderReceipt(order_id=order_id, quantity=1)

    @rpc
    async def list_orders(self) -> list[OrderReceipt]:
        return [OrderReceipt(order_id="retail:widget", quantity=2)]

    @rpc
    async def cancel(self, order_id: str) -> None:
        pass

    @rpc
    async def label(self, str: int) -> str:
        return f"Order {str}"
