"""The answer to `{service}.describe`: this service's Description, in canonical bytes."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import TYPE_CHECKING, Any

from ..error_text import may_expose
from ..extension import RejectMessage, WorkerContext
from ..validation import CONTENT_TYPE_JSON
from .replies import answer

if TYPE_CHECKING:
    from .rpc import RpcDispatcher


class DescribeAnswers:
    """Answers describe requests for the RPC dispatcher `owner`, reading its config, logger,
    worker and service when each request arrives, and keeping the last body built."""

    def __init__(self, owner: RpcDispatcher) -> None:
        self._owner = owner
        self._built: tuple[Any, str] | None = None

    async def answer(self, msg: Any) -> None:
        """Answer this service's Description metadata in canonical bytes."""
        from cliffracer.introspect import canonical, describe

        owner = self._owner
        headers = dict(msg.headers) if getattr(msg, "headers", None) else {}
        ctx = WorkerContext(
            kind="describe",
            subject=msg.subject,
            headers=headers,
            correlation_id=None,
            payload={},
            raw=msg,
        )

        target_service = owner.service or owner

        async def call() -> Any:
            # Built once and then served as the same bytes. The key is the class
            # and the whole config, which is everything describe() reads, so a
            # config assigned to after the first request is described afresh.
            # A config that cannot be fingerprinted is never cached.
            try:
                key: Any = (type(target_service), owner.config.model_dump_json())
            except Exception:
                key = None
            if key is not None and self._built is not None and self._built[0] == key:
                return self._built[1]
            body = canonical(
                describe(
                    type(target_service),
                    service=owner.config.name,
                    version=owner.config.version,
                    config=owner.config,
                ).to_dict()
            )
            if key is not None:
                self._built = (key, body)
            return body

        has_reply = bool(getattr(msg, "reply", True))
        try:
            body = await self._owner._run_worker(ctx, call)
            if has_reply:
                try:
                    await answer(
                        msg,
                        body.encode(),
                        content_type=CONTENT_TYPE_JSON,
                        correlation_id=ctx.correlation_id,
                    )
                except Exception as e:
                    owner.logger.debug(f"Failed to send describe reply: {e}")
        except RejectMessage as e:
            if has_reply:
                # Same distinction as the RPC dispatcher's refusal arm; describe answers on an
                # unauthenticated subject, so getting it wrong here is louder.
                crashed = e.hook_crash
                try:
                    await answer(
                        msg,
                        json.dumps(
                            {
                                "success": False,
                                "error": str(e) if crashed else f"refused: {e}",
                                "code": "internal" if crashed else "refused",
                                "timestamp": datetime.now(UTC).isoformat(),
                                "correlation_id": ctx.correlation_id,
                            }
                        ).encode(),
                        content_type=CONTENT_TYPE_JSON,
                        correlation_id=ctx.correlation_id,
                    )
                except Exception as reply_err:
                    owner.logger.debug(f"Failed to send describe refusal reply: {reply_err}")
        except Exception as e:
            owner.logger.error(f"Error answering describe for {owner.config.name}: {e}")
            if has_reply:
                # The same policy the RPC dispatcher applies to a handler's error, and
                # rendered here rather than through `exception_text` so the
                # exposed form keeps the class name for an exception carrying
                # no message: `exception_text` would answer "ValueError: " for
                # a bare `ValueError()`, and the name alone is the more useful
                # half. `{service}.describe` is subscribed by every service and
                # answered with no authentication, so this is the widest reach
                # any of these arms has.
                if may_expose(owner.config):
                    error = str(e) or e.__class__.__name__
                else:
                    error = f"Internal server error (correlation_id: {ctx.correlation_id})"
                try:
                    await answer(
                        msg,
                        json.dumps(
                            {
                                "success": False,
                                "error": error,
                                "code": "internal",
                                "timestamp": datetime.now(UTC).isoformat(),
                                "correlation_id": ctx.correlation_id,
                            }
                        ).encode(),
                        content_type=CONTENT_TYPE_JSON,
                        correlation_id=ctx.correlation_id,
                    )
                except Exception as reply_err:
                    owner.logger.debug(f"Failed to send describe error reply: {reply_err}")
