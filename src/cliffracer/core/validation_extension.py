"""Validate RPC payloads against handler type annotations.

RPC payloads are validated against handler parameter annotations using a synthesized
Pydantic model. Validation errors are caught and communicated via `RejectMessage`
during `worker_setup`. Because arbitrary exceptions in `worker_setup` are swallowed
to protect dispatch stability, `RejectMessage` is the required mechanism to halt
execution and return a validation error response.

Detailed validation error structures are stored in `ctx.data["validation_error"]`,
allowing the RPC dispatcher to format structured field error responses.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError

from .extension import Extension, RejectMessage, WorkerContext


class ValidationExtension(Extension):
    """Validates an RPC payload against the handler's own annotations."""

    fails_closed: bool = True

    async def worker_setup(self, ctx: WorkerContext) -> None:
        if ctx.kind not in ("rpc", "async_rpc"):
            return
        name = ctx.data.get("handler_name")
        specs = getattr(self.service, "_rpc_specs", None)
        if specs is None and hasattr(self.service, "container"):
            specs = getattr(getattr(self.service, "container", None), "_rpc_specs", None)
            if specs is None:
                specs = getattr(
                    getattr(getattr(self.service, "container", None), "registry", None),
                    "rpc_specs",
                    None,
                )
        specs = specs or {}
        spec = specs.get(name) if name else None
        if spec is None:
            return

        raw_payload: Any = ctx.payload
        if not isinstance(raw_payload, dict):
            try:
                spec.payload_model.model_validate(raw_payload)
            except ValidationError as exc:
                ctx.data["validation_error"] = exc
                raise RejectMessage("validation failed") from exc
            else:
                raise RejectMessage("validation failed: payload must be an object")

        payload = dict(ctx.payload)
        # Every caller puts correlation_id in the body (`call_rpc` does it
        # unconditionally), so under extra="forbid" it would be an extra key on
        # every single call. It is transport, not a parameter; dispatch passes
        # it to the handlers that declare one.
        payload.pop("correlation_id", None)
        try:
            model = spec.payload_model.model_validate(payload)
        except ValidationError as exc:
            # The structure the envelope needs cannot ride on RejectMessage's
            # string reason, so it goes here and the dispatcher reads it.
            ctx.data["validation_error"] = exc
            raise RejectMessage("validation failed") from exc

        ctx.data["validated_kwargs"] = {p.name: getattr(model, p.name) for p in spec.params}
