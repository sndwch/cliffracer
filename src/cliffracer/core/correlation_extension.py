"""Correlation id per message, as the first extension on every chain."""

from .correlation import CorrelationContext, correlation_id_var
from .extension import Extension, WorkerContext


class CorrelationExtension(Extension):
    """Sets the correlation id for one dispatch and resets it afterwards.

    PER MESSAGE, never from the ambient context: the id comes from the message
    (the context's own, then its headers, then its payload) or is a new one. The
    dispatchers run each message in a task of its own, and a task takes a copy of the
    context, so what is set here cannot reach the subscription callback or the next
    message through the context. Reading the ambient id would still let a dispatch
    driven inline, from inside another task, inherit that task's id.
    ``worker_teardown`` always runs and resets the variable, so a handler that raises
    leaves nothing stamped for the work that follows it in the same task.
    """

    async def worker_setup(self, ctx: WorkerContext) -> None:
        ctx.correlation_id = CorrelationContext.for_message(
            ctx.headers, ctx.payload, ctx.correlation_id
        )
        ctx.data["_correlation_token"] = correlation_id_var.set(ctx.correlation_id)

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        token = ctx.data.pop("_correlation_token", None)
        if token is not None:
            correlation_id_var.reset(token)
