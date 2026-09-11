"""The shapes the fixture service speaks. Deliberately not flat.

Nested models, a list of them, an optional, a literal and two defaults: the
combination a generated client either reproduces exactly or gets wrong in a way
a round trip will show.
"""

from typing import Literal

from pydantic import BaseModel


class Line(BaseModel):
    sku: str
    qty: int = 1


class Order(BaseModel):
    order_id: str | None = None
    lines: list[Line]
    priority: Literal["low", "high"] = "low"


class Receipt(BaseModel):
    order_id: str
    total_qty: int
    tags: dict[str, str] = {}
