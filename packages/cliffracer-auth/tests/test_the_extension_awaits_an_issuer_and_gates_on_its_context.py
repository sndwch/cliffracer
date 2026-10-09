"""`AuthExtension` works with any issuer, and what it admits is decided by the context it returns.

`AuthExtension` accepts "any object with a `validate_token(token)` method". An issuer that does
I/O (a key fetch, a remote introspection, a database lookup) is naturally `async def`; its result
used to be a coroutine object that nothing awaited, so every request was refused and the log
showed an `AttributeError` about `'coroutine'`. Now the result is awaited.

The extension also gates on `context.is_authenticated`: for `SimpleAuthService` that is redundant
(PyJWT already enforces `exp`), so it is the duck-typed issuers it exists for, and nothing drove
one that returns a context with a past or missing `expires_at`, or with no user.
"""

import gc
import json
import warnings
from datetime import UTC, datetime, timedelta
from unittest.mock import AsyncMock

import pytest
from cliffracer_auth import AuthConfig, AuthExtension
from cliffracer_auth.simple_auth import AuthContext, AuthUser

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

USER = AuthUser(user_id="u1", username="issuer-user", email="u@example.com")


def _context(*, expires_in: timedelta | None = timedelta(hours=1), user: AuthUser | None = USER):
    expires_at = None if expires_in is None else datetime.now(UTC) + expires_in
    return AuthContext(user=user, token="t", expires_at=expires_at)


class SyncIssuer:
    def __init__(self, context):
        self.context = context

    def validate_token(self, token):
        return self.context


class AsyncIssuer:
    def __init__(self, context):
        self.context = context

    async def validate_token(self, token):
        return self.context


class RaisingAsyncIssuer:
    async def validate_token(self, token):
        raise RuntimeError("the introspection endpoint is down")


async def _dispatch(issuer) -> tuple[dict, list[str]]:
    """One authenticated-looking request through a real dispatch: the reply and the handler log."""
    ran: list[str] = []

    class Svc(CliffracerService):
        auth = AuthExtension(issuer)

        @rpc
        async def hello(self) -> str:
            ran.append("ran")
            return "hello"

    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject = "a.rpc.hello"
    msg.data = json.dumps({}).encode()
    msg.headers = {"authorization": "Bearer some-token"}
    await svc.container._handle_rpc_request(msg)
    return json.loads(msg.respond.call_args.args[0]), ran


async def test_CONTROL_a_synchronous_issuer_is_admitted():
    reply, ran = await _dispatch(SyncIssuer(_context()))

    assert reply.get("result") == "hello", reply
    assert ran == ["ran"]


async def test_an_async_issuer_is_awaited_and_admits_the_caller():
    reply, ran = await _dispatch(AsyncIssuer(_context()))

    assert reply.get("result") == "hello", reply
    assert ran == ["ran"]


async def test_an_async_issuer_that_returns_none_refuses_the_caller():
    reply, ran = await _dispatch(AsyncIssuer(None))

    assert reply["error"] == "refused: unauthenticated", reply
    assert ran == []


async def test_an_async_issuer_that_raises_refuses_without_leaking_internals():
    reply, ran = await _dispatch(RaisingAsyncIssuer())

    assert ran == []
    assert "coroutine" not in json.dumps(reply), reply
    assert "introspection endpoint" not in json.dumps(reply), reply
    assert reply["error"].startswith("extension auth failed"), reply


async def test_awaiting_the_issuer_leaves_no_coroutine_unawaited():
    """Judged on the issuer's own coroutine. `gc.collect()` finalises every unreachable object in
    the process, so a coroutine an earlier test left unawaited is reported in this window too; it
    is collected before the window opens, and only a warning naming the issuer's coroutine counts."""
    gc.collect()
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        await _dispatch(AsyncIssuer(_context()))
        gc.collect()

    issuers = [
        str(w.message)
        for w in caught
        if "never awaited" in str(w.message)
        and AsyncIssuer.validate_token.__qualname__ in str(w.message)
    ]
    assert not issuers, issuers


@pytest.mark.parametrize(
    "context",
    [
        pytest.param(_context(expires_in=-timedelta(hours=1)), id="expired-an-hour-ago"),
        pytest.param(_context(expires_in=None), id="no-expires-at"),
        pytest.param(_context(user=None), id="no-user"),
    ],
)
@pytest.mark.parametrize("issuer_class", [SyncIssuer, AsyncIssuer], ids=["sync", "async"])
async def test_an_issuer_that_returns_an_unauthenticated_context_is_refused(issuer_class, context):
    """The README's contract: a context carrying a user and a FUTURE `expires_at`, or None."""
    reply, ran = await _dispatch(issuer_class(context))

    assert reply["error"] == "refused: unauthenticated", reply
    assert ran == []


def test_enable_auth_is_not_a_config_field():
    """It was documented as switching authentication on or off and read by nothing."""
    assert "enable_auth" not in AuthConfig.model_fields
    assert not hasattr(AuthConfig(secret_key="x" * 40), "enable_auth")
