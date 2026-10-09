"""`AuthExtension` refuses when something it did not expect goes wrong, and says what it is.

`fails_closed` is the backstop under the promise that a handler that runs has been
authenticated: an exception out of `worker_setup` that the extension did not catch makes the
dispatch refuse the message instead of running the handler. The only test of it used a
synthetic extension. These drive it on `AuthExtension` itself.
"""

import json
from unittest.mock import AsyncMock

import pytest
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

SECRET = "s" * 40


def test_the_extension_declares_that_it_fails_closed():
    assert AuthExtension.fails_closed is True


def test_the_class_has_a_docstring_that_says_what_it_does():
    doc = AuthExtension.__doc__
    assert doc is not None, "the class docstring is not the first statement of the class body"
    assert "bearer token" in doc and "AuthContext" in doc


async def test_an_unexpected_failure_in_worker_setup_refuses_the_message_and_the_handler_never_runs(
    monkeypatch,
):
    ran: list[bool] = []

    class Svc(CliffracerService):
        auth = AuthExtension(SimpleAuthService(AuthConfig(secret_key=SECRET)))

        @rpc
        async def work(self) -> dict[str, bool]:
            ran.append(True)
            return {"ok": True}

    def explode(self, headers):
        raise RuntimeError("a parse failure the extension did not anticipate")

    monkeypatch.setattr(AuthExtension, "_token_from", explode)
    svc = Svc(ServiceConfig(name="a"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = AsyncMock()
    msg.subject = "a.rpc.work"
    msg.data = json.dumps({}).encode()
    msg.headers = {"authorization": "Bearer anything"}

    await svc.container._handle_rpc_request(msg)

    reply = json.loads(msg.respond.call_args.args[0])
    assert ran == [], "the handler ran after the extension failed"
    assert reply["success"] is False, reply
    assert reply["error"] == "extension auth failed: internal error", reply
    assert "parse failure" not in json.dumps(reply), "the cause belongs in the log, not on the wire"
