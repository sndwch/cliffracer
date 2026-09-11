"""JWT verification on the per-message hook chain."""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime

from loguru import logger

from cliffracer.core.extension import Extension, RejectMessage, WorkerContext
from cliffracer_auth.simple_auth import (
    AuthContext,
    AuthUser,
    SimpleAuthService,
    auth_context_var,
)

_BEARER = "bearer "


class AuthExtension(Extension):
    fails_closed: bool = True

    """Verify a bearer token on every dispatch and publish the AuthContext.

    Declared on the service like any other extension::

        class Orders(CliffracerService):
            auth = AuthExtension(SimpleAuthService(AuthConfig(secret_key=...)))

            @rpc
            async def create(self, order: dict) -> dict:
                who = get_current_user()           # or get_current_context()

    A handler that reaches its body has been authenticated: `worker_setup`
    raises `RejectMessage` before the handler runs, and the container's error
    path turns that into the RPC error response.
    """

    def __init__(
        self,
        auth: SimpleAuthService,
        header: str = "authorization",
        *,
        allow_timers: bool = True,
        default_timer_user: AuthUser | None = None,
    ) -> None:
        self.auth = auth
        # Headers are compared case-folded: NATS carries whatever the publisher
        # wrote, and "Authorization" is as likely as "authorization".
        self.header = header.lower()
        self.allow_timers = allow_timers
        self.default_timer_user = default_timer_user

    async def worker_setup(self, ctx: WorkerContext) -> None:
        token = self._token_from(ctx.headers or {})
        if ctx.kind == "timer" and token is None:
            if not self.allow_timers:
                raise RejectMessage("unauthenticated")
            if self.default_timer_user is not None:
                timer_context = AuthContext(
                    user=self.default_timer_user,
                    expires_at=datetime.max.replace(tzinfo=UTC),
                )
                ctx.data["auth"] = timer_context
                ctx.data["_auth_token"] = auth_context_var.set(timer_context)
            return

        if token is None:
            raise RejectMessage("unauthenticated")

        # Fail closed with RejectMessage if auth backend raises an exception.
        try:
            context: AuthContext | None = self.auth.validate_token(token)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - any backend failure is a refusal
            logger.warning(f"{self.name}: token validation raised, refusing: {exc!r}")
            raise RejectMessage("unauthenticated") from exc

        if context is None or not context.is_authenticated:
            # An expired or revoked token is unauthenticated.
            raise RejectMessage("unauthenticated")

        ctx.data["auth"] = context
        # Set AuthContext in contextvars for handlers; reset in worker_teardown.
        ctx.data["_auth_token"] = auth_context_var.set(context)

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        token = ctx.data.pop("_auth_token", None)
        if token is not None:
            auth_context_var.reset(token)

    def _token_from(self, headers: dict[str, str]) -> str | None:
        for key, value in headers.items():
            if key.lower() != self.header:
                continue
            if not value:
                return None
            if value.lower().startswith(_BEARER):
                return value[len(_BEARER) :].strip() or None
            return value.strip() or None
        return None
