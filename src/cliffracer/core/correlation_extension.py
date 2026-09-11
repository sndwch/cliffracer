"""Correlation id per message, as the first extension on every chain."""

from .correlation import CorrelationContext, correlation_id_var
from .extension import Extension, WorkerContext


class CorrelationExtension(Extension):
    """Sets the correlation id for one dispatch and resets it afterwards.

    PER MESSAGE, never from the ambient context: the subscription callback is
    long-lived and its context outlives any one message, so reading an existing
    id here would make every later message inherit the first one's permanently.
    ``worker_teardown`` always runs, so a handler that raises cannot
    leave its id stamped on the next message.
    """

    async def worker_setup(self, ctx: WorkerContext) -> None:
        cid = (
            ctx.correlation_id
            or CorrelationContext.extract_from_headers(ctx.headers)
            or (ctx.payload.get("correlation_id") if isinstance(ctx.payload, dict) else None)
        )
        ctx.correlation_id = CorrelationContext.new_id_unless_given(cid)
        ctx.data["_correlation_token"] = correlation_id_var.set(ctx.correlation_id)

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        token = ctx.data.pop("_correlation_token", None)
        if token is not None:
            correlation_id_var.reset(token)
