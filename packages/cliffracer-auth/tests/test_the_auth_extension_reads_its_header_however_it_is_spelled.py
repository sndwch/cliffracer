"""`AuthExtension` finds the token whichever way the publisher spelled the header.

NATS carries the header exactly as written, and `Authorization` is as likely as
`authorization`. The extension folds the configured name and every incoming key, and reads
the value as a bearer token whatever the case of the scheme. Every other test builds its
message with the lowercase spelling and the default header, so removing the folds broke no
test and no test constructed the extension with a header of its own.
"""

import pytest
from cliffracer_auth import AuthConfig, AuthExtension, SimpleAuthService
from cliffracer_auth.simple_auth import auth_context_var

from cliffracer.core.extension import RejectMessage, WorkerContext

pytestmark = pytest.mark.unit

SECRET = "s" * 40


@pytest.fixture
def auth():
    service = SimpleAuthService(AuthConfig(secret_key=SECRET))
    service.create_user("alice", "alice@example.com", "pw12345678")
    return service


@pytest.fixture
def token(auth):
    issued = auth.authenticate("alice", "pw12345678")
    assert issued is not None
    return issued


async def _who(extension: AuthExtension, headers: dict[str, str]) -> str:
    """The user a dispatch with these headers runs as, or `refused`."""
    ctx = WorkerContext(
        kind="rpc", subject="svc.rpc.work", headers=headers, correlation_id=None, payload={}
    )
    try:
        await extension.worker_setup(ctx)
    except RejectMessage:
        return "refused"
    try:
        return str(ctx.data["auth"].user.username)
    finally:
        await extension.worker_teardown(ctx)
        assert auth_context_var.get() is None


@pytest.mark.parametrize(
    "name, scheme",
    [
        ("authorization", "Bearer"),
        ("Authorization", "Bearer"),
        ("AUTHORIZATION", "Bearer"),
        ("authorization", "bearer"),
        ("Authorization", "bEaReR"),
    ],
)
async def test_the_default_header_is_found_in_any_case_with_the_scheme_in_any_case(
    auth, token, name, scheme
):
    assert await _who(AuthExtension(auth), {name: f"{scheme} {token}"}) == "alice"


async def test_surrounding_space_is_ignored(auth, token):
    assert await _who(AuthExtension(auth), {"authorization": f"Bearer   {token}  "}) == "alice"


async def test_a_value_with_no_scheme_is_read_as_the_bare_token(auth, token):
    """Characterisation: the header is the token channel, so a bare token is accepted."""
    assert await _who(AuthExtension(auth), {"authorization": token}) == "alice"


@pytest.mark.parametrize(
    "headers",
    [
        {},
        {"authorization": ""},
        {"authorization": "Bearer"},
        {"authorization": "Bearer "},
        {"authorization": "Bearer    "},
        {"authorization": "Basic YWxpY2U6cHc="},
        {"authorization": "Bearer not-a-token"},
        {"x-other": "Bearer anything"},
    ],
)
async def test_a_missing_empty_or_unusable_value_is_refused(auth, headers):
    assert await _who(AuthExtension(auth), headers) == "refused"


async def test_the_header_name_the_extension_was_given_is_folded_and_is_the_one_read(auth, token):
    extension = AuthExtension(auth, "X-Auth-Token")

    assert extension.header == "x-auth-token"
    for spelling in ("x-auth-token", "X-Auth-Token", "X-AUTH-TOKEN"):
        assert await _who(extension, {spelling: f"Bearer {token}"}) == "alice"
        assert await _who(extension, {spelling: token}) == "alice"


async def test_a_custom_header_extension_does_not_read_the_default_header(auth, token):
    extension = AuthExtension(auth, "X-Auth-Token")

    assert await _who(extension, {"authorization": f"Bearer {token}"}) == "refused"


async def test_CONTROL_the_default_extension_does_not_read_a_custom_header(auth, token):
    assert await _who(AuthExtension(auth), {"x-auth-token": f"Bearer {token}"}) == "refused"
