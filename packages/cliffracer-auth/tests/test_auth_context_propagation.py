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

    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("a.rpc.admin_only", _token(auth_service, "bob"))
    await svc.container._handle_rpc_request(msg)

    # The caller authenticated and lacks the role: refused as forbidden, which is not the
    # `unauthenticated` a missing or unusable token gets.
    body = _reply(msg)
    assert (body.get("code"), body.get("error")) == ("refused", "refused: forbidden"), body
    assert "unauthenticated" not in body.get("error", "")


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
    import contextvars

    seen: list[str] = []
    teardown_errors: list[BaseException] = []

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

    # The pipeline swallows a hook's exception and logs it, so the exception is captured where it
    # is raised, on the bound extension, rather than found again in the log line.
    bound = svc.auth
    real_teardown = bound.worker_teardown

    async def recording_teardown(ctx):
        try:
            await real_teardown(ctx)
        except BaseException as exc:
            teardown_errors.append(exc)
            raise

    bound.worker_teardown = recording_teardown

    a = _msg("a.rpc.slow", _token(auth_service, "alice"))
    b = _msg("a.rpc.slow", _token(auth_service, "bob"))
    # Each dispatch runs as its own task, which runs in a copy of the context it was started
    # from, so the test's own context cannot show a leak whether or not teardown resets the
    # variable. The contexts the tasks run in are therefore handed in and read back.
    first, second = contextvars.copy_context(), contextvars.copy_context()
    await asyncio.gather(
        asyncio.create_task(svc.container._handle_rpc_request(a), context=first),
        asyncio.create_task(svc.container._handle_rpc_request(b), context=second),
    )

    assert sorted(seen) == ["alice", "bob"], seen
    for msg in (a, b):
        assert "error" not in _reply(msg), _reply(msg)
    assert first.get(auth_context_var) is None and second.get(auth_context_var) is None, (
        "a dispatch left its authenticated context set in the context it ran in"
    )
    assert teardown_errors == [], teardown_errors

    # Verify worker_teardown did not raise (the container would have swallowed it).
    assert not teardown_errors, (
        "worker_teardown raised and the container swallowed it: "
        f"{teardown_errors!r}. A reset Token stored on the extension instance is "
        "created in one dispatch's context and reset in another's."
    )


async def test_a_backend_that_raises_stops_the_handler_and_is_reported_as_ours():
    """An unreachable backend fails closed, but it is not the caller's fault.

    The handler must not run -- that half is the point of `fails_closed` and is
    unchanged. What the caller is TOLD changed: a backend that raises has said
    nothing about this token, which may be perfectly valid, so answering
    "refused: unauthenticated" sends them to check credentials for a fault that
    is ours. The extension lets the exception out, the pipeline synthesises
    `RejectMessage(hook_crash=True)`, and the wire reports a fault.

    On JetStream the same change is the difference between the event being
    acknowledged and destroyed and being redelivered, then dead-lettered.
    """
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

    reply = _reply(msg)
    assert not reached, "the handler ran for a caller the backend never authenticated"
    assert reply.get("code") == "internal", reply
    assert "auth" in reply.get("error", ""), reply
    assert "unauthenticated" not in reply.get("error", ""), (
        "an unreachable backend told us nothing about this token; naming the "
        "caller unauthenticated sends them to check credentials for our fault"
    )
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
