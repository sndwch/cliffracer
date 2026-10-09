"""Decomposed message dispatching subsystem."""

from .dlq import DeadLetterPublisher
from .events import DispatchOutcome, EventDispatcher, _HandlerMeta
from .handler_limits import HandlerLimits, default_max_queued
from .jetstream import JetStreamDispatcher, _JetStreamHeartbeat
from .outbound import OutboundDispatcher
from .pipeline import ExtensionPipeline
from .rpc import RpcDispatcher

__all__ = [
    "DispatchOutcome",
    "_HandlerMeta",
    "_JetStreamHeartbeat",
    "ExtensionPipeline",
    "HandlerLimits",
    "default_max_queued",
    "DeadLetterPublisher",
    "RpcDispatcher",
    "EventDispatcher",
    "JetStreamDispatcher",
    "OutboundDispatcher",
]
