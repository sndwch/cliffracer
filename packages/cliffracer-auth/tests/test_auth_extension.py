"""A handler runs only for an authenticated caller.

Driven through the REAL dispatch (`_handle_rpc_request`), not by calling
`worker_setup` directly: the claim is that an unauthenticated message never
reaches the handler and that the caller gets an error saying so, and both of
those are properties of the container's hook chain rather than of the hook.
Calling the hook by hand would assert that a raise raises.
"""

import json
from unittest.mock import AsyncMock

import pytest
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig, rpc

pytestmark = pytest.mark.unit

# SimpleAuthService refuses a secret under 32 characters (simple_auth.py:96),
# which is a guard worth having and worth not working around with a shorter
# one in tests.
SECRET = "test-secret-not-a-real-one-0123456789abcdef"


def _service():
    auth = SimpleAuthService(AuthConfig(secret_key=SECRET))
    auth.create_user(
        username="ana", email="ana@example.invalid", password="pw-ana-12345", roles={"user"}
    )

    class Svc(CliffracerService):
        auth_ext = AuthExtension(auth)

        @rpc
        async def whoami(self) -> str:
            return "reached"

    svc = Svc(ServiceConfig(name="s"))
    return svc, auth


def _msg(subject, data, headers):
    m = AsyncMock()
    m.subject = subject
    m.data = json.dumps(data).encode()
    m.headers = headers
    return m


def _replies(msg) -> list[dict]:
    return [json.loads(c.args[0].decode()) for c in msg.respond.await_args_list]


async def test_a_valid_token_reaches_the_handler():
    svc, auth = _service()
    await svc.container._setup_extensions()
    svc._discover_handlers()

    token = auth.authenticate("ana", "pw-ana-12345")
    assert token, "the fixture user must be able to authenticate"

    msg = _msg("s.rpc.whoami", {}, {"authorization": f"Bearer {token}"})
    await svc.container._handle_rpc_request(msg)

    replies = _replies(msg)
    assert replies, "the handler must answer"
    assert replies[0].get("result") == "reached", replies[0]


async def test_a_missing_token_gets_an_unauthenticated_error():
    svc, _auth = _service()
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _msg("s.rpc.whoami", {}, {})
    await svc.container._handle_rpc_request(msg)

    replies = _replies(msg)
    assert replies, "an unauthenticated call must still get an answer"
    body = json.dumps(replies[0])
    assert "unauthenticated" in body, body
    assert replies[0].get("result") != "reached", replies[0]


async def test_a_garbage_token_is_also_unauthenticated():
    """CONTROL for the case above: without it, "the error says unauthenticated"
    would also pass on an extension that refused every message including valid
    ones -- which the first test rules out from the other side."""
    svc, _auth = _service()
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _msg("s.rpc.whoami", {}, {"authorization": "Bearer not-a-real-token"})
    await svc.container._handle_rpc_request(msg)

    body = json.dumps(_replies(msg)[0])
    assert "unauthenticated" in body, body
