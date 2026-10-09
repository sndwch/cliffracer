"""Order-entry fixtures for RPC validation diagnostics."""

import asyncio
import json
import re
import traceback

from pydantic import BaseModel, ConfigDict, ValidationError, field_validator
from pydantic_core import PydanticCustomError

from cliffracer import CliffracerService, rpc
from cliffracer.core.extension import Extension

CANARY = "credential_canary_orders"


class PurchaseOrder(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)
    units: int
    authorization: str = "approved"
    allocations: dict[str, int] = {}

    @field_validator("authorization")
    @classmethod
    def check_authorization(cls, value):
        mode, _, detail = value.partition(":")
        if mode == "reject":
            raise ValueError(detail)
        if mode == "crash":
            raise RuntimeError(detail)
        if mode == "custom":
            raise PydanticCustomError(detail, "Rejected {credential}", {"credential": detail})
        return value


class DiagnosticCapture(Extension):
    async def setup(self, ctx):
        self.records = []
        self.ready = asyncio.Event()

    async def worker_result(self, ctx, result, exc):
        error = ctx.data.get("validation_error")
        chain = []
        pending = [exc] if exc is not None else []
        visited = set()
        while pending:
            current = pending.pop()
            if id(current) in visited:
                continue
            visited.add(id(current))
            chain.append(
                {
                    "text": str(current),
                    "details": json.loads(current.json())
                    if isinstance(current, ValidationError)
                    else None,
                }
            )
            # Both links matter, even when traceback formatting suppresses one.
            pending.extend(
                linked for linked in (current.__cause__, current.__context__) if linked is not None
            )
        self.records.append(
            {
                "details": json.loads(error.json()) if error is not None else None,
                "chain": chain,
                "traceback": "".join(traceback.format_exception(exc)) if exc else "",
            }
        )
        self.ready.set()


class OrderService(CliffracerService):
    diagnostics = DiagnosticCapture()

    @rpc
    async def place(self, order: PurchaseOrder) -> int:
        self.accepted.append(order.units)
        return order.units


def invalid_orders():
    """Distinct payload-derived Pydantic diagnostic surfaces."""
    return [
        ("root", CANARY),
        ("missing", {"order": {"authorization": CANARY}}),
        ("input", {"order": {"units": CANARY}}),
        ("outer_key", {"order": {"units": 1}, CANARY: "value"}),
        ("model_key", {"order": {"units": 1, CANARY: "value"}}),
        ("dictionary_key", {"order": {"units": 1, "allocations": {CANARY: "invalid"}}}),
        ("validator", {"order": {"units": 1, "authorization": f"reject:{CANARY}"}}),
        ("custom_type", {"order": {"units": 1, "authorization": f"custom:{CANARY}"}}),
    ]


def diagnostic_canaries(value):
    """Recognize complete synthetic credential tokens in serialized diagnostics."""
    return set(re.findall(r"\bcredential_canary_[a-z]+\b", json.dumps(value, default=str)))
