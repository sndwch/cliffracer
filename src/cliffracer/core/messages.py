"""Base message models and envelope schemas."""

from datetime import UTC, datetime
from typing import Any

from pydantic import BaseModel, Field


class Message(BaseModel):
    """Base message class for all service communications.

    These fields belong to the message, not to any one publish of it. A model
    published with `publish_event` or `broadcast_message` travels in the event
    envelope's `data`, and its `timestamp` is carried there unchanged; the
    envelope's own top-level `timestamp` and `source_service` say which service
    sent that message and when. A listener is handed `data`, so a message that
    is received and published again keeps its original values. See "The event
    envelope" in docs/api-reference.md.

    `correlation_id` is the exception: it is the correlation id of the request
    or event the message belongs to, and the framework's own record of it is
    the envelope's `correlation_id` and the message headers, never `data`. A
    publisher takes a model's value as the correlation id of the publish. A
    `Message` the framework builds for a handler, or takes back from one as a
    result, has the field filled from that id when it is `None`. A value set
    explicitly is left alone, and where the two differ the envelope's is the
    one the framework acts on.
    """

    timestamp: datetime = Field(default_factory=lambda: datetime.now(UTC))
    correlation_id: str | None = None


def with_correlation_id(value: Any, correlation_id: str | None) -> Any:
    """`value` with its `correlation_id` filled, if it is a `Message` that has none.

    Anything else, a `Message` whose id was set explicitly, or a missing
    `correlation_id` comes back unchanged. Only `Message` is filled, because it
    declares the field as `str | None`: a model of another kind may declare a
    field of that name with a type a correlation id is not.
    """
    if correlation_id and isinstance(value, Message) and value.correlation_id is None:
        return value.model_copy(update={"correlation_id": correlation_id})
    return value


class RPCRequest(Message):
    """Base class for RPC requests"""

    pass


class RPCResponse(Message):
    """Base class for RPC responses"""

    success: bool = True
    error: str | None = None
    details: list[dict] | None = None


class BroadcastMessage(Message):
    """Base class for broadcast messages.

    `source_service` is the service that produced the message. A service that
    relays it does not overwrite it: the relay's name goes in the envelope's
    top-level `source_service`, and this one stays in `data`, which is what a
    listener reads.
    """

    source_service: str
