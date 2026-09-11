"""Base message models and envelope schemas."""

from datetime import UTC, datetime

from pydantic import BaseModel, Field


class Message(BaseModel):
    """Base message class for all service communications"""

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str | None = None


class RPCRequest(Message):
    """Base class for RPC requests"""

    pass


class RPCResponse(Message):
    """Base class for RPC responses"""

    success: bool = True
    error: str | None = None
    details: list[dict] | None = None


class BroadcastMessage(Message):
    """Base class for broadcast messages"""

    source_service: str
