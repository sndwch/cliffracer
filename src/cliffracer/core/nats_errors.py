"""What nats-py raises when a message cannot be sent, as the framework's own errors.

The standalone `ServiceClient` and a service's own `call_rpc`, `call_async`, `call_rpc_no_wait`,
`publish_event` and `broadcast_message` translate through this module, so the same failure
reaches a caller as the same class whichever API sent it. A handler's `except RpcError` holds it.
"""

from collections.abc import Awaitable

from nats import errors

from .exceptions import RpcClientError, RpcConnectionError, RpcError

# What a publish meets. A request also meets a timeout and no responders, which each caller
# translates itself. The JetStream errors a publish raises (`nats.js.errors`) are not in this
# list: they say what the stream did, which a caller may act on.
PUBLISH_ERRORS: tuple[type[errors.Error], ...] = (
    errors.MaxPayloadError,
    errors.OutboundBufferLimitError,
    errors.ConnectionDrainingError,
    errors.ConnectionClosedError,
    errors.StaleConnectionError,
)


def rpc_error_for(exc: errors.Error, subject: str, *, awaiting_reply: bool) -> RpcError:
    """The `RpcError` for a nats-py error raised on a send to `subject`.

    An argument over the broker's `max_payload` is the caller's, so it is an `RpcClientError`: no
    retry on any connection can send it. Everything else is a failure of the connection, an
    `RpcConnectionError`, whose message says what happened to `subject`.
    """
    if isinstance(exc, errors.MaxPayloadError):
        return RpcClientError(
            f"the arguments for {subject} are larger than the broker accepts in one message "
            f"(its max_payload)"
        )
    if isinstance(exc, errors.OutboundBufferLimitError):
        return RpcConnectionError(
            f"{subject} could not be sent: the connection's buffer is full while it reconnects"
        )
    if isinstance(exc, errors.ConnectionDrainingError):
        return RpcConnectionError(f"{subject} could not be sent: the connection is draining")
    if isinstance(exc, errors.ConnectionClosedError | errors.StaleConnectionError):
        if awaiting_reply:
            return RpcConnectionError(f"the connection was lost before {subject} could be answered")
        return RpcConnectionError(f"{subject} could not be sent: the connection was lost")
    return RpcConnectionError(f"{subject} could not be sent: nats-py raised {type(exc).__name__}")


async def publish_mapped[T](publishing: Awaitable[T], subject: str) -> T:
    """Await a publish to `subject`, raising what the connection raises for it as an `RpcError`."""
    try:
        return await publishing
    except PUBLISH_ERRORS as exc:
        raise rpc_error_for(exc, subject, awaiting_reply=False) from exc
