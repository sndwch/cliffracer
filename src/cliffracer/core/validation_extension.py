"""Validate RPC payloads against handler type annotations.

RPC payloads are validated against handler parameter annotations using a synthesized
Pydantic model. Pydantic failures are communicated via `RejectMessage` during
`worker_setup` and carry a structured validation diagnostic. Unexpected validator
exceptions fail closed as internal errors through the extension pipeline.

Policy-selected validation error structures are stored in `ctx.data["validation_error"]`,
allowing the RPC dispatcher to format structured field error responses.
"""

from __future__ import annotations

from typing import Any

from pydantic import ValidationError
from pydantic_core import PydanticCustomError

from .extension import Extension, RejectMessage, WorkerContext
from .validation import validate_payload


def redacts_rpc_validation(config: Any) -> bool:
    """Whether RPC ingestion diagnostics must omit payload-derived content."""
    return getattr(config, "rpc_validation_errors", "full") == "redacted"


def _redacted_validation_error() -> ValidationError:
    """A Pydantic diagnostic with no input, field names, or validator text."""
    return ValidationError.from_exception_data(
        "RPC payload",
        [
            {
                "type": PydanticCustomError("validation_failed", "Invalid RPC payload"),
                "loc": (),
                "input": None,
            }
        ],
        hide_input=True,
    )


class ValidationExtension(Extension):
    """Validates an RPC payload against the handler's own annotations."""

    fails_closed: bool = True

    async def worker_setup(self, ctx: WorkerContext) -> None:
        if ctx.kind not in ("rpc", "async_rpc"):
            return
        name = ctx.data.get("handler_name")
        if not name:
            # A context that names no handler has nothing to validate for.
            return
        # The registry the dispatcher reads, and only that one: the two sides cannot
        # disagree about which handlers have a spec. A named handler with none is
        # refused, not dispatched unvalidated: this extension fails closed.
        spec = self.service.container.registry.rpc_specs.get(name)
        if spec is None:
            raise RuntimeError(f"no payload spec is registered for RPC handler {name!r}")

        failure: Exception | None = None
        try:
            model = validate_payload(spec.payload_model, ctx.payload)
        except ValidationError as exc:
            failure = (
                _redacted_validation_error() if redacts_rpc_validation(self.service.config) else exc
            )
        except Exception:
            if not redacts_rpc_validation(self.service.config):
                raise
            failure = RuntimeError("RPC payload validation failed")

        # Raise outside the except block so redacted failures have no original
        # exception in __context__, including when a validator raises directly.
        if isinstance(failure, ValidationError):
            ctx.data["validation_error"] = failure
            raise RejectMessage("validation failed") from failure
        if failure is not None:
            raise failure

        ctx.data["validated_kwargs"] = {p.name: getattr(model, p.name) for p in spec.params}
