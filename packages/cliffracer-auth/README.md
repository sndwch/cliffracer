# cliffracer-auth

JWT authentication for cliffracer services, on the per-message hook chain.

Declare it like any other extension. Verification runs in `worker_setup`, before
the handler. A handler that reaches its body has been authenticated.

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService, get_current_user

class Orders(CliffracerService):
    auth = AuthExtension(SimpleAuthService(AuthConfig(secret_key=SECRET)))

    @rpc
    async def create(self, item: str) -> dict[str, str]:
        return {"item": item, "by": get_current_user().username}
```

`secret_key` must be at least 32 characters.

The bearer token is read from the `authorization` header. The header name is
compared in lower case, so a publisher may set `Authorization` or
`authorization`.

Three things are refused: a message with no token, a message with an invalid
one, and a message whose backend raised while checking. All three get
`refused: unauthenticated`.

## Authorization

`requires_auth`, `requires_roles` and `requires_permissions` read the identity
the extension established:

```python
@rpc
@requires_roles("admin")
async def delete_everything(self) -> None: ...
```

A caller holding a token but lacking the role gets `Required roles: ('admin',)`.

The identity lives in a contextvar. The extension sets it in `worker_setup` and
resets it in `worker_teardown`, and carries the reset token in that dispatch's
own `ctx.data`. Keeping the token per dispatch is what makes concurrent
dispatches safe: a token set in one dispatch's context and reset in another's
raises inside `worker_teardown`.

## Refusing a message

The extension refuses by raising `RejectMessage`. That is core's rejection
channel, and the one hook exception that stops the handler running. Core
swallows every other hook exception so that a faulty extension leaves dispatch
working, which is why a refusal needs its own channel.

The container turns a refusal into the RPC error response, runs `worker_result`
with it, and acks the message. A refusal is policy working.

## Using a different issuer

`AuthExtension` accepts any object with a `validate_token(token)` method.
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
