# cliffracer-auth

JWT authentication for cliffracer services, on the per-message hook chain.

Declare it like any other extension. Verification runs in `worker_setup`, before
the handler. A handler that reaches its body has been authenticated, with one
exception: a timer firing that carries no token (see Timers below).

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService, get_current_user

SECRET = "a-signing-key-of-at-least-32-characters"

class Orders(CliffracerService):
    auth = AuthExtension(SimpleAuthService(AuthConfig(secret_key=SECRET)))

    @rpc
    async def create(self, item: str) -> dict[str, str]:
        return {"item": item, "by": get_current_user().username}
```

`secret_key` must be at least 32 characters.

`leeway_seconds` (default `0`) forgives that many seconds of clock skew between the host that mints a token and the one that verifies it. It applies to `iat` and to `exp`, so it also keeps every token valid that many seconds past its expiry; set it only where several hosts share the key. It is a `SecretStr`: printing, logging or dumping an `AuthConfig` shows a mask, and `config.secret_key.get_secret_value()` reads the key. A plain `str` is accepted wherever it is set.

`token_expiry_hours` (default `24`) is the lifetime of a token this service mints, and the longest
one it accepts: a token whose `exp` is more than `token_expiry_hours` (plus `leeway_seconds`) after
its `iat` is refused, and so is a token with no `iat`. Every host that shares the key must use the
same `token_expiry_hours` and `refresh_max_lifetime_hours`, or smaller ones. Those bounds are what
keep a revoked chain revoked. Another host does not see this one's revocations and may go on
refreshing the chain, so a revocation is held until a lifetime past the chain's refresh cap (its
`oiat` plus `refresh_max_lifetime_hours`). With `refresh_max_lifetime_hours=None`, a revoked chain
is kept for the life of the process, so memory grows with the number of revocations.

## Password hashes

`SimpleAuthService` stores a password as `pbkdf2_sha256$<iterations>$<salt>$<hash>`, made at
`AuthConfig.pbkdf2_iterations` (100,000 by default). Two constants bound that count, and the
config and `verify_password` share both, so a configuration can never create a user the verifier
then refuses:

- `MIN_PBKDF2_ITERATIONS = 1_000` is the fewest. A stored record made at fewer iterations is
  refused: `verify_password` returns `False` and logs a warning that names the record's count and
  this floor, and the user's password has to be set again. A configured count below it is refused
  when the config is built.
- `MAX_PBKDF2_ITERATIONS = 10_000_000` is the most `verify_password` will run for a stored record.

A record between the floor and the configured count still verifies, and is written again at the
configured count the next time its user logs in. A refused record costs what a verify costs, so
the time a login takes does not say which users hold one.

`AuthExtension` keeps the exact issuer object passed to it. Revoking a token
through that object affects the next validation on every bound service. New
users and role or permission changes remain available for subsequent
authentication and refresh operations. This is intentional shared security
state; the extension's own per-service lifecycle state remains isolated.

The bearer token is read from the `authorization` header. The header name is
compared in lower case, so a publisher may set `Authorization` or
`authorization`.

Two things are refused: a message with no token and a message with an invalid
one. Both get `refused: unauthenticated`. A message whose backend raised while
checking is not a refusal: the service is broken, not the caller wrong, so the
caller gets an `internal` error (`extension auth failed: internal error`) and the
log has the cause.

## Authorization

`requires_auth`, `requires_roles` and `requires_permissions` read the identity
the extension established:

```python
@rpc
@requires_roles("admin")
async def delete_everything(self) -> None: ...
```

A caller holding a token but lacking the role is refused, and the handler does not run. The
reply is `refused: forbidden` with `code: "refused"`, and a caller with no usable token gets
`refused: unauthenticated`. Neither names the roles or permissions the handler requires, under
any setting of `expose_internal_errors`: that text is in the service's log, as one warning line
without a traceback. A denied fire-and-forget request or event is refused the same way, so a
durable listener acknowledges it and does not redeliver it, and a metrics extension counts it
as a refusal. The decorators still raise `AuthenticationError` / `AuthorizationError` to a
caller of the function itself, and to a `@timer` firing, which has no sender to refuse.

Several names mean any one of them: `@requires_roles("admin", "support")` admits a caller with
either role, and `@requires_permissions("orders:read", "orders:write")` a caller with either
permission. To require both, stack the decorators. A decorator with no names, or with a list
instead of separate strings, raises `ConfigurationError` where it is applied.

The identity lives in a contextvar. The extension sets it in `worker_setup` and
resets it in `worker_teardown`, and carries the reset token in that dispatch's
own `ctx.data`. Keeping the token per dispatch is what makes concurrent
dispatches safe: a token set in one dispatch's context and reset in another's
raises inside `worker_teardown`.

## HTTP requests

`AuthMiddleware` does for an HTTP request what the extension does for a message.
`app.middleware("http")(AuthMiddleware(auth_service))` authenticates each request from
its `Authorization: Bearer <token>` header and sets the same identity for the decorators
while the handler runs, then restores whatever was set before. The header name and the
`Bearer` scheme are matched without regard to case. A request with no valid token
proceeds anonymously, and a decorated handler refuses it.

## Timers

A `@timer` firing is not a message from a caller, so it carries no bearer token unless it
is given one. By default (`allow_timers=True`) a timer with no token is let through with no
identity: the handler runs, `get_current_user()` is `None`, and a `@requires_roles` handler
raises `AuthenticationError` on every firing. This is the one exception to "every handler
requires a valid token". Three ways to give a timer a say:

```python
from cliffracer import CliffracerService, timer
from cliffracer_auth import AuthConfig, AuthExtension, AuthUser, SimpleAuthService

issuer = SimpleAuthService(AuthConfig(secret_key="a-signing-key-of-at-least-32-characters"))
cleanup_user = AuthUser(
    user_id="timer", username="timer", email="timer@example.com", roles={"cleanup"}
)


class RefusesTimers(CliffracerService):
    # 1. refuse every tokenless timer firing
    auth = AuthExtension(issuer, allow_timers=False)


class TimersRunAsAServiceUser(CliffracerService):
    # 2. run tokenless timers as a named service user
    auth = AuthExtension(issuer, default_timer_user=cleanup_user)


class OneTimerPresentsAToken(CliffracerService):
    auth = AuthExtension(issuer)

    # 3. give one timer a token to present
    @timer(interval=60, token_factory=lambda: issuer.authenticate("service", "password") or "")
    async def sweep(self) -> None: ...
```

`@timer(headers={"authorization": "Bearer ..."})` passes a fixed header the same way.

A timer's token travels in its `authorization` header, and the extension reads it there whichever
header it is configured to read from messages (`AuthExtension(issuer, header="x-api-token")`): a
timer firing is the service's own clock, so it is not held to the sender's header. A message that
carries a token under `authorization` is still read by the configured header only.

## Calling another authenticated service

A service that calls another authenticated service sends a token for its own identity. Give the
extension `outbound_token_factory`, a callable that returns the token (or an awaitable of it), the
way `token_factory` does for a timer:

```python
class Orders(CliffracerService):
    auth = AuthExtension(
        issuer,
        outbound_token_factory=lambda: issuer.authenticate("orders", "a-long-service-password"),
    )

    @rpc
    async def place(self, sku: str) -> dict[str, str]:
        return await self.call_rpc("inventory", "reserve", sku=sku)   # carries the token
```

Every `call_rpc`, `call_async`, `call_rpc_no_wait`, `publish_event` and `broadcast_message` then
carries `Authorization: Bearer <token>`, under the extension's configured `header`. The factory is
called for each message sent, so a token that expires is minted afresh. Without a factory nothing
is attached. A header the message already carries is left as it is; a factory that returns
`None` or an empty string sends the message without one; a factory that raises is logged and the
message goes without one, because a send hook cannot cancel a send, so the receiving service
answers `unauthenticated`. The token is the service's own: the caller's token, which the handler
was given, is never forwarded.

## Refusing a message

The extension refuses by raising `RejectMessage`. That is core's rejection
channel, and the one hook exception that stops the handler running. Core
swallows every other hook exception so that a faulty extension leaves dispatch
working, which is why a refusal needs its own channel.

The container turns a refusal into the RPC error response, runs `worker_result`
with it, and acks the message. A refusal is policy working.

## Using a different issuer

`AuthExtension` accepts any object with a `validate_token(token)` method, synchronous or
`async def` (an issuer that does I/O is awaited).
`SimpleAuthService` is one. Swap it to verify against another issuer and the
hook chain, the refusal path and the decorators stay as they are. The parameter
is annotated `SimpleAuthService`; the annotation is the narrow part, and duck
typing works.

Return an `AuthContext` carrying a user **and** a future `expires_at`, or return
`None`.

The extension gates on `context.is_authenticated`, which is
`user is not None and is_valid`, and `is_valid` requires `expires_at` to be set
and in the future. An issuer must set `expires_at` in the future for authentication
to succeed; otherwise requests are refused as unauthenticated.

Raising from `validate_token` is safe. The extension turns the exception into a
refusal and logs it under the extension's name.

Installed from PyPI, versioned in lockstep with `cliffracer`.
