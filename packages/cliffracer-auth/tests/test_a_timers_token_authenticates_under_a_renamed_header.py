"""A timer's `token_factory` token authenticates whichever header the extension reads.

`@timer(token_factory=...)` sends its token as the `authorization` header, and says so. An
`AuthExtension` configured with another `header` reads that header, so the timer's token was never
looked at: the firing was refused as `unauthenticated` under `allow_timers=False`, and under
`allow_timers=True` it ran as nobody. A timer is not a network sender, so the extension reads the
timer's own header for a timer firing as well as the one it is configured to read, and only for one.
"""

import pytest
from cliffracer_auth import AuthConfig, get_current_context
from cliffracer_auth.extension import AuthExtension
from cliffracer_auth.simple_auth import SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig, rpc, timer
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit

SECRET = "test-secret-not-a-real-one-0123456789abcdef"


@pytest.fixture
def issuer():
    auth = SimpleAuthService(AuthConfig(secret_key=SECRET))
    auth.create_user(username="svc", email="s@example.com", password="password123")
    return auth


async def _fire(issuer, *, header: str, allow_timers: bool, token: str | None):
    """Run one firing of a timer that records who it ran as; `(who, the timer)`."""
    seen: list[str] = []

    class Svc(CliffracerService):
        ext = AuthExtension(issuer, header=header, allow_timers=allow_timers)

        @timer(interval=3600, token_factory=(lambda: token) if token is not None else None)
        async def tick(self):
            context = get_current_context()
            seen.append(context.user.username if context and context.user else "<no identity>")

    svc = Svc(ServiceConfig(name="timer-header", health_port=0))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    await svc.container._start_timers()
    (fired,) = svc.container.registry.timers
    try:
        await fired._execute_method()
    finally:
        await svc.container._stop_timers()
    return seen, fired


async def test_a_timers_token_authenticates_under_a_renamed_header_when_timers_need_one(issuer):
    token = issuer.authenticate("svc", "password123")

    seen, fired = await _fire(issuer, header="x-api-token", allow_timers=False, token=token)

    assert seen == ["svc"], (seen, fired.last_refusal)
    assert fired.refusal_count == 0


async def test_a_timers_token_is_the_identity_it_runs_as_under_a_renamed_header(issuer):
    token = issuer.authenticate("svc", "password123")

    seen, _ = await _fire(issuer, header="x-api-token", allow_timers=True, token=token)

    assert seen == ["svc"]


async def test_an_invalid_timer_token_is_refused_under_a_renamed_header(issuer):
    seen, fired = await _fire(issuer, header="x-api-token", allow_timers=True, token="not-a-token")

    assert seen == []
    assert fired.last_refusal == "unauthenticated"


async def test_CONTROL_the_default_header_authenticates_a_timer_as_before(issuer):
    token = issuer.authenticate("svc", "password123")

    seen, _ = await _fire(issuer, header="authorization", allow_timers=False, token=token)

    assert seen == ["svc"]


async def test_CONTROL_a_timer_with_no_token_is_still_refused_when_timers_need_one(issuer):
    seen, fired = await _fire(issuer, header="x-api-token", allow_timers=False, token=None)

    assert seen == []
    assert fired.last_refusal == "unauthenticated"


async def test_CONTROL_a_timer_with_no_token_still_runs_with_no_identity_when_timers_are_allowed(
    issuer,
):
    seen, _ = await _fire(issuer, header="x-api-token", allow_timers=True, token=None)

    assert seen == ["<no identity>"]


async def test_CONTROL_a_message_carrying_the_timers_header_is_not_authenticated_by_it(issuer):
    """The fallback is for a timer firing: a sender cannot use it to skip the configured header."""
    token = issuer.authenticate("svc", "password123")

    class Svc(CliffracerService):
        ext = AuthExtension(issuer, header="x-api-token")

        @rpc
        async def whoami(self) -> str:
            return "reached the handler"

    async with ServiceTestHarness(Svc, config=ServiceConfig(name="who", health_port=0)) as harness:
        wrong_header = await harness.rpc("whoami", headers={"authorization": f"Bearer {token}"})
        right_header = await harness.rpc("whoami", headers={"x-api-token": f"Bearer {token}"})

    assert wrong_header.data["error"] == "refused: unauthenticated"
    assert right_header.data["result"] == "reached the handler"
