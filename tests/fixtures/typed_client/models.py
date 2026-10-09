"""The shapes the fixture service speaks. Deliberately not flat.

Models held inside models, a list of them, an optional, a literal and two
defaults: the combination a generated client either reproduces exactly or gets
wrong in a way a round trip will show.

`Shipment.Leg` is nested in the OTHER sense -- a model class declared inside
another class, so its qualname is dotted. That is a different thing from
`Order` holding `list[Line]`, and the two were conflated here: this docstring
said "nested models" of the composition case, the class-nesting case had no
coverage anywhere in the tree, and an emitter bug that made every dotted
qualname generate an unimportable client survived because the word made it look
covered.
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


class Shipment(BaseModel):
    """Holds a model class DECLARED INSIDE IT, so `Leg`'s qualname is dotted.

    The emitted client must import `Shipment` and write `Shipment.Leg`; it used
    to invent a name nothing bound and fail at import.
    """

    class Leg(BaseModel):
        carrier: str
        eta_days: int = 2

    legs: list[Leg] = []
