"""Tests ensuring AuthContext propagates from AuthExtension to RPC handlers and decorators."""

import json
from unittest.mock import AsyncMock

import pytest
from cliffracer_auth import (
    AuthConfig,
    AuthExtension,
    SimpleAuthService,
    get_current_context,
    get_current_user,
    requires_roles,
)
from cliffracer_auth.simple_auth import auth_context_var

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

SECRET = "s" * 40


@pytest.fixture
def auth_service():
    svc = SimpleAuthService(AuthConfig(secret_key=SECRET))
    svc.create_user("alice", "alice@example.com", "pw12345678", roles={"admin"})
    svc.create_user("bob", "bob@example.com", "pw12345678", roles={"guest"})
    return svc


def _token(auth_service, user: str) -> str:
    result = auth_service.authenticate(user, "pw12345678")
    return getattr(result, "access_token", result)


def _msg(subject: str, token: str | None):
    m = AsyncMock()
    m.subject = subject
    m.data = json.dumps({}).encode()
    m.headers = {"authorization": f"Bearer {token}"} if token else {}
    return m


def _reply(msg) -> dict:
    return json.loads(msg.respond.call_args.args[0])


async def test_get_current_user_is_the_caller_inside_the_handler(auth_service):
    seen = {}

    class Svc(CliffracerService):
        auth = AuthExtension(auth_service)

        @rpc
        async def whoami(self) -> dict[str, bool]:
            seen["user"] = get_current_user()
            seen["context"] = get_current_context()
            return {"ok": True}

    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    await svc.container._handle_rpc_request(_msg("a.rpc.whoami", _token(auth_service, "alice")))

    assert "user" in seen, "the handler did not run"
    assert seen["context"] is not None, "the authenticated context did not reach the handler"
    assert seen["user"].username == "alice"


async def test_a_requires_roles_handler_runs_for_a_caller_with_the_role(auth_service):
    class Svc(CliffracerService):
        auth = AuthExtension(auth_service)

        @rpc
        @requires_roles("admin")
        async def admin_only(self) -> dict[str, bool]:
            return {"ok": True}

    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("a.rpc.admin_only", _token(auth_service, "alice"))
    await svc.container._handle_rpc_request(msg)

    body = _reply(msg)
    assert "error" not in body, body
    assert body["result"] == {"ok": True}


async def test_the_same_handler_refuses_a_caller_without_the_role(auth_service):
    """The negative. Without it, an extension that authenticated everyone as an
    admin would satisfy the case above."""

    class Svc(CliffracerService):
        auth = AuthExtension(auth_service)

        @rpc
        @requires_roles("admin")
        async def admin_only(self) -> dict[str, bool]:
            return {"ok": True}

    svc = Svc(ServiceConfig(name="a", expose_internal_errors=True))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("a.rpc.admin_only", _token(auth_service, "bob"))
    await svc.container._handle_rpc_request(msg)

    # AuthorizationError confirms role validation failure.
    body = _reply(msg)
    assert body.get("error") == "Required roles: ('admin',)", body
    assert "Authentication required" not in body.get("error", "")


async def test_the_context_does_not_leak_to_the_next_message(auth_service):
    """Verify that authentication context is scoped to individual dispatches."""
    after = {}

    class Svc(CliffracerService):
        auth = AuthExtension(auth_service)

        @rpc
        async def whoami(self) -> dict[str, bool]:
            return {"ok": True}

    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    assert auth_context_var.get() is None
    await svc.container._handle_rpc_request(_msg("a.rpc.whoami", _token(auth_service, "alice")))
    after["outside"] = auth_context_var.get()

    assert after["outside"] is None, (
        "the authenticated context outlived its dispatch; the next message on "
        "this subscription would inherit it"
    )


async def test_an_unauthenticated_message_never_reaches_the_handler(auth_service):
    """Verify unauthenticated requests are refused before reaching the handler."""
    reached = []

    class Svc(CliffracerService):
        auth = AuthExtension(auth_service)

        @rpc
        async def whoami(self) -> dict[str, bool]:
            reached.append(True)
            return {"ok": True}

    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("a.rpc.whoami", None)
    await svc.container._handle_rpc_request(msg)

    assert not reached
    assert auth_context_var.get() is None
    assert _reply(msg).get("error") == "refused: unauthenticated", _reply(msg)


async def test_two_concurrent_dispatches_do_not_break_the_reset(auth_service):
    """Verify concurrent dispatches isolate contextvar tokens without teardown errors."""
    import asyncio

    from loguru import logger

    seen: list[str] = []
    logged: list[str] = []
    sink = logger.add(logged.append, level="ERROR", format="{message}")

    class Svc(CliffracerService):
        auth = AuthExtension(auth_service)

        @rpc
        async def slow(self) -> dict[str, bool]:
            await asyncio.sleep(0.02)
            user = get_current_user()
            seen.append(user.username if user else None)
            return {"ok": True}

    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    a = _msg("a.rpc.slow", _token(auth_service, "alice"))
    b = _msg("a.rpc.slow", _token(auth_service, "bob"))
    try:
        await asyncio.gather(
            svc.container._handle_rpc_request(a), svc.container._handle_rpc_request(b)
        )
    finally:
        logger.remove(sink)

    assert sorted(seen) == ["alice", "bob"], seen
    for msg in (a, b):
        assert "error" not in _reply(msg), _reply(msg)
    assert auth_context_var.get() is None

    # Verify worker_teardown did not swallow unexpected exceptions.
    swallowed = [line for line in logged if "worker_teardown raised" in line]
    assert not swallowed, (
        "worker_teardown raised and the container swallowed it: "
        f"{swallowed}. A reset Token stored on the extension instance is "
        "created in one dispatch's context and reset in another's."
    )


async def test_a_backend_that_raises_refuses_rather_than_letting_the_handler_run():
    """Verify backend validation exceptions result in request refusal."""
    reached = []

    class RaisingIssuer:
        def validate_token(self, token):
            raise RuntimeError("issuer unreachable")

    class Svc(CliffracerService):
        auth = AuthExtension(RaisingIssuer())

        @rpc
        async def whoami(self) -> str:
            reached.append(True)
            return "NO-IDENTITY"

    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("a.rpc.whoami", "anything")
    await svc.container._handle_rpc_request(msg)

    assert not reached, "the handler ran for a caller the backend never authenticated"
    assert _reply(msg).get("error") == "refused: unauthenticated", _reply(msg)
    assert auth_context_var.get() is None


async def test_CONTROL_a_working_backend_is_not_refused(auth_service):
    """Verify valid credentials authenticate successfully."""

    class Svc(CliffracerService):
        auth = AuthExtension(auth_service)

        @rpc
        async def whoami(self) -> str:
            return get_current_user().username

    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("a.rpc.whoami", _token(auth_service, "alice"))
    await svc.container._handle_rpc_request(msg)

    assert _reply(msg)["result"] == "alice", _reply(msg)
