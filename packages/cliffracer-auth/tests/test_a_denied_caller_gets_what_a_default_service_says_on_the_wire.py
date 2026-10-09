"""What a service tells a caller the auth decorators turned away, and what it logs.

A denial raised by `@requires_auth`, `@requires_roles` or `@requires_permissions` is a refusal,
the same as the extension's `refused: unauthenticated`: the caller is told `code: "refused"`
and not "the service broke", the role names stay off the wire under every setting of
`expose_internal_errors`, and the log carries one line naming what was required, with no
traceback. The decorators still raise `AuthenticationError` / `AuthorizationError` to whoever
calls the function directly, and a timer firing, which has no caller to refuse, still records
the original error (pinned in `test_auth_timer_interaction.py`).
"""

import json
from unittest.mock import AsyncMock

import pytest
from cliffracer_auth import (
    AuthConfig,
    AuthExtension,
    SimpleAuthService,
    requires_auth,
    requires_roles,
)
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.extension import Extension, RejectMessage

pytestmark = pytest.mark.unit

SECRET = "s" * 40


@pytest.fixture
def auth():
    service = SimpleAuthService(AuthConfig(secret_key=SECRET))
    service.create_user("alice", "alice@example.com", "pw12345678", roles={"admin"})
    service.create_user("bob", "bob@example.com", "pw12345678", roles={"guest"})
    return service


def _service_class(issuer):
    class Svc(CliffracerService):
        auth = AuthExtension(issuer)

        @rpc
        @requires_roles("admin")
        async def admin_only(self) -> dict[str, bool]:
            return {"ok": True}

        @rpc
        @requires_auth
        async def any_user(self) -> dict[str, bool]:
            return {"ok": True}

        @listener("events.guarded", fanout=True)
        @requires_roles("admin")
        async def on_guarded(self) -> None:
            self.ran = True

    return Svc


async def _service(auth, **config):
    svc = _service_class(auth)(ServiceConfig(name="a", **config))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    return svc


def _message(auth, user: str | None, subject: str) -> AsyncMock:
    msg = AsyncMock()
    msg.subject = subject
    msg.data = json.dumps({}).encode()
    msg.headers = {}
    if user is not None:
        msg.headers = {"authorization": f"Bearer {auth.authenticate(user, 'pw12345678')}"}
    return msg


async def _call(auth, user: str | None, method: str = "admin_only", **config) -> dict:
    svc = await _service(auth, **config)
    msg = _message(auth, user, f"a.rpc.{method}")
    await svc.container._handle_rpc_request(msg)
    return json.loads(msg.respond.call_args.args[0])


def _records_while(level: str):
    """A context that collects the loguru records at `level` and above."""

    class _Sink:
        def __enter__(self):
            self.records: list[dict] = []
            self._id = logger.add(lambda message: self.records.append(message.record), level=level)
            return self

        def __exit__(self, *exc):
            logger.remove(self._id)

    return _Sink()


async def test_a_caller_without_the_role_is_refused_not_told_the_service_crashed(auth):
    reply = await _call(auth, "bob")

    assert reply["success"] is False
    assert reply["code"] == "refused"
    assert reply["error"] == "refused: forbidden"


@pytest.mark.parametrize("config", [{}, {"expose_internal_errors": True}])
async def test_the_reply_never_names_the_roles_the_handler_requires(auth, config):
    reply = await _call(auth, "bob", **config)

    assert "admin" not in json.dumps(reply)
    assert "Required roles" not in json.dumps(reply)
    assert "traceback" not in reply


async def test_a_caller_with_the_role_is_served(auth):
    reply = await _call(auth, "alice")

    assert reply["success"] is True and reply["result"] == {"ok": True}


async def test_a_caller_with_no_token_is_refused_not_told_the_service_crashed(auth):
    reply = await _call(auth, None)

    assert reply["success"] is False
    assert reply["code"] == "refused"
    assert reply["error"] == "refused: unauthenticated"


async def test_a_requires_auth_denial_with_no_identity_says_unauthenticated():
    """`@requires_auth` on a service with no AuthExtension sees no identity at all."""

    class Bare(CliffracerService):
        @rpc
        @requires_auth
        async def any_user(self) -> dict[str, bool]:
            return {"ok": True}

    svc = Bare(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _message(None, None, "a.rpc.any_user")
    await svc.container._handle_rpc_request(msg)
    reply = json.loads(msg.respond.call_args.args[0])

    assert (reply["code"], reply["error"]) == ("refused", "refused: unauthenticated")


async def test_a_role_denial_is_logged_once_without_a_traceback_and_names_what_was_required(auth):
    with _records_while("WARNING") as seen:
        await _call(auth, "bob")

    (denial,) = seen.records
    assert denial["level"].name == "WARNING"
    assert denial["exception"] is None
    assert "admin_only" in denial["message"]
    assert "Required roles: ('admin',)" in denial["message"]


async def test_a_role_denial_leaves_nothing_at_error_level(auth):
    with _records_while("ERROR") as seen:
        await _call(auth, "bob")

    assert [r["message"] for r in seen.records] == []


async def test_a_denied_fire_and_forget_request_is_a_refusal_not_a_failure(auth):
    svc = await _service(auth)
    msg = _message(auth, "bob", "a.async.admin_only")

    with _records_while("DEBUG") as seen:
        await svc.container._handle_async_request(msg)

    levels = {r["level"].name for r in seen.records if "admin_only" in r["message"]}
    assert "ERROR" not in levels, [r["message"] for r in seen.records]
    assert "WARNING" in levels
    assert all(r["exception"] is None for r in seen.records)


async def test_a_denied_event_is_refused_and_does_not_reach_the_error_path(auth):
    """With `raise_on_error` a failed handler raises; a refusal is answered, not raised."""
    svc = await _service(auth)
    svc.ran = False
    msg = _message(auth, "bob", "events.guarded")

    with _records_while("ERROR") as seen:
        await svc.container.dispatcher.handle_event(msg, raise_on_error=True)

    assert svc.ran is False
    assert [r["message"] for r in seen.records] == []


async def test_a_permitted_event_still_runs(auth):
    svc = await _service(auth)
    svc.ran = False

    await svc.container.dispatcher.handle_event(
        _message(auth, "alice", "events.guarded"), raise_on_error=True
    )

    assert svc.ran is True


class _Recorder(Extension):
    """What a `worker_result` hook is handed, which is what a metrics extension counts from."""

    def __init__(self) -> None:
        self.excs: list[BaseException | None] = []

    async def worker_result(self, ctx, result, exc):
        self.excs.append(exc)


async def test_the_result_hooks_see_the_denial_as_a_refusal(auth):
    class Watched(_service_class(auth)):
        recorder = _Recorder()

    svc = Watched(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    await svc.container._handle_rpc_request(_message(auth, "bob", "a.rpc.admin_only"))

    (seen,) = svc.recorder.excs
    assert isinstance(seen, RejectMessage)
    assert seen.reason == "forbidden"
