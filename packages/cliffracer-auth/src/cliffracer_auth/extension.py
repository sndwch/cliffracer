"""JWT verification on the per-message hook chain."""

from __future__ import annotations

import inspect
from collections.abc import Awaitable, Callable
from datetime import UTC, datetime
from typing import cast

from cliffracer.core.credentials import bearer
from cliffracer.core.extension import Extension, RejectMessage, SharedDependency, WorkerContext
from cliffracer.core.timer import TIMER_TOKEN_HEADER
from cliffracer_auth.simple_auth import (
    AuthContext,
    AuthUser,
    SimpleAuthService,
    auth_context_var,
)

_BEARER = "bearer "


def _is_constructed_issuer(value: object) -> bool:
    """Whether value already implements the auth issuer contract."""
    return callable(getattr(value, "validate_token", None))


class AuthExtension(Extension):
    """Verify a bearer token on every dispatch and publish the AuthContext.

    Declared on the service like any other extension::

        class Orders(CliffracerService):
            auth = AuthExtension(SimpleAuthService(AuthConfig(secret_key=...)))

            @rpc
            async def create(self, order: dict) -> dict:
                who = get_current_user()           # or get_current_context()

    A handler that reaches its body has been authenticated: `worker_setup`
    raises `RejectMessage` before the handler runs, and the container's error
    path turns that into the RPC error response. The one exception is a timer
    firing with no token while `allow_timers` is True (the default): it runs
    with no identity, or with `default_timer_user` if one is given.

    A service that calls another authenticated service gives `outbound_token_factory`, a callable
    that returns a token for the service's own identity::

        auth = AuthExtension(issuer, outbound_token_factory=lambda: issuer.authenticate("orders", pw))

    Every call, async call and published event then carries `Authorization: Bearer <that token>`
    (under the configured `header`), the factory being called once for each. Nothing is forwarded
    from the message being handled: the caller's token is never sent on.
    """

    # An exception out of `worker_setup` that the extension did not catch refuses the message
    # rather than letting the handler run.
    fails_closed: bool = True

    def __new__(cls, *args: object, **kwargs: object) -> AuthExtension:
        """Keep the configured issuer live across extension binding.

        The auth backend owns revocations and user state.  Binding a service
        must therefore preserve its identity instead of deep-copying a stale
        snapshot as ordinary extension configuration does.
        """
        # A zero-argument callable given to an extension is called once per bound service, and its
        # result is what the extension receives. The token factory is called once per message sent,
        # so it is passed through as the callable.
        factory = kwargs.get("outbound_token_factory")
        if callable(factory) and not isinstance(factory, SharedDependency):
            kwargs["outbound_token_factory"] = SharedDependency(factory)
        if "auth" in kwargs:
            auth = kwargs["auth"]
            if _is_constructed_issuer(auth):
                kwargs["auth"] = SharedDependency(auth)
        elif args:
            auth = args[0]
            if _is_constructed_issuer(auth):
                args = (SharedDependency(auth), *args[1:])
        return cast(AuthExtension, super().__new__(cls, *args, **kwargs))

    def __init__(
        self,
        auth: SimpleAuthService | SharedDependency[SimpleAuthService],
        header: str = "authorization",
        *,
        allow_timers: bool = True,
        default_timer_user: AuthUser | None = None,
        outbound_token_factory: Callable[[], str | None | Awaitable[str | None]]
        | SharedDependency[Callable[[], str | None | Awaitable[str | None]]]
        | None = None,
    ) -> None:
        self.auth = auth.value if isinstance(auth, SharedDependency) else auth
        # Headers are compared case-folded: NATS carries whatever the publisher
        # wrote, and "Authorization" is as likely as "authorization".
        self.header = header.lower()
        self.allow_timers = allow_timers
        self.default_timer_user = default_timer_user
        self.outbound_token_factory = (
            outbound_token_factory.value
            if isinstance(outbound_token_factory, SharedDependency)
            else outbound_token_factory
        )

    async def worker_setup(self, ctx: WorkerContext) -> None:
        token = self._token_from(ctx.headers or {})
        if token is None and ctx.kind == "timer" and self.header != TIMER_TOKEN_HEADER:
            # A timer's `token_factory` sends its token as `authorization`, whichever header this
            # extension reads from a message. A timer is the service's own clock and not a sender, so
            # nothing is gained by refusing the header it documents, and a renamed header would
            # otherwise leave the token unread. Only for a timer: a message cannot use it.
            token = self._token_from(ctx.headers or {}, TIMER_TOKEN_HEADER)
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

        # NOT converted to a refusal. A backend that raises has told us nothing
        # about this token -- it may be perfectly valid -- so calling it
        # "unauthenticated" sends the caller to look at their own credentials
        # for a fault that is ours, and on JetStream it acknowledged the event
        # and destroyed it.
        #
        # `fails_closed` is True, so letting this out still means the handler
        # does not run: the pipeline catches it, logs it under this extension's
        # name and synthesises `RejectMessage(hook_crash=True)`. Failing closed
        # is preserved; what changes is that the failure is reported as ours.
        #
        # An issuer that does I/O (a JWKS fetch, a remote introspection, a database lookup) is
        # naturally `async def`; its result is awaited here. A synchronous issuer is called as
        # before.
        validated = self.auth.validate_token(token)
        context: AuthContext | None = (
            await validated if inspect.isawaitable(validated) else validated
        )

        if context is None or not context.is_authenticated:
            # An expired or revoked token is unauthenticated.
            raise RejectMessage("unauthenticated")

        ctx.data["auth"] = context
        # Set AuthContext in contextvars for handlers; reset in worker_teardown.
        ctx.data["_auth_token"] = auth_context_var.set(context)

    async def before_call(self, ctx: WorkerContext) -> None:
        """Attach the service's own token to an outbound call, when a factory was given.

        A header the message already carries is left as it is. A factory that returns nothing sends
        the call without one, and a factory that raises is logged and the call goes without one: a
        send hook cannot refuse the call, so the service that receives it answers "unauthenticated".
        """
        if self.outbound_token_factory is None:
            return
        if any(key.lower() == self.header for key in ctx.headers):
            return
        try:
            minted = self.outbound_token_factory()
            token = await minted if inspect.isawaitable(minted) else minted
        except Exception as exc:
            self._service_log.error(
                f"{self.name}: outbound_token_factory raised {type(exc).__name__}: {exc}; "
                f"{ctx.kind} {ctx.subject} is sent without a token"
            )
            return
        if token:
            ctx.headers[self.header] = bearer(token)

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        token = ctx.data.pop("_auth_token", None)
        if token is not None:
            auth_context_var.reset(token)

    def _token_from(self, headers: dict[str, str], header: str | None = None) -> str | None:
        wanted = header or self.header
        for key, value in headers.items():
            if key.lower() != wanted:
                continue
            if not value:
                return None
            if value.lower().startswith(_BEARER):
                return value[len(_BEARER) :].strip() or None
            return value.strip() or None
        return None
