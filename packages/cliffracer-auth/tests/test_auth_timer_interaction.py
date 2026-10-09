"""Tests for Timer and AuthExtension interaction."""

import pytest
from cliffracer_auth import (
    AuthConfig,
    AuthContext,
    AuthUser,
    get_current_context,
    requires_roles,
)
from cliffracer_auth.extension import AuthExtension
from cliffracer_auth.simple_auth import SimpleAuthService

from cliffracer import CliffracerService, ServiceConfig, timer
from cliffracer.testing import FakeClock, ServiceTestHarness

pytestmark = pytest.mark.unit

SECRET = "test-secret-not-a-real-one-0123456789abcdef"


async def _one_interval(svc: CliffracerService) -> None:
    """Start the service's 0.01 s timer on a fake clock, move the clock one interval, stop it."""
    clock = FakeClock()
    harness = ServiceTestHarness(svc)
    try:
        await harness.start_timers(clock=clock)
        await clock.advance(0.01)
    finally:
        await harness.teardown()


@pytest.fixture
def auth_service():
    auth = SimpleAuthService(AuthConfig(secret_key=SECRET))
    auth.create_user(
        username="timer_runner",
        email="runner@example.com",
        password="password123",
        roles={"worker", "scheduler"},
    )
    return auth


@pytest.mark.asyncio
async def test_timer_allowed_by_default_without_token(auth_service):
    """By default, allow_timers=True allows timer execution without authentication headers."""
    executed = False

    class TimerService(CliffracerService):
        auth = AuthExtension(auth_service)

        @timer(interval=0.01)
        async def on_tick(self):
            nonlocal executed
            executed = True

    svc = TimerService(ServiceConfig(name="timer-svc"))
    await _one_interval(svc)

    assert executed is True


@pytest.mark.asyncio
async def test_timer_rejected_when_allow_timers_is_false(auth_service):
    """When allow_timers=False, a timer firing carries no token and is refused: `RejectMessage`."""
    executed = False

    class StrictTimerService(CliffracerService):
        auth = AuthExtension(auth_service, allow_timers=False)

        @timer(interval=0.01)
        async def on_tick(self):
            nonlocal executed
            executed = True

    svc = StrictTimerService(ServiceConfig(name="strict-timer-svc"))
    await _one_interval(svc)

    # The handler did not run, and the timer says why: the firing was refused, which the timer
    # counts apart from an error. A timer that never fired would leave `executed` False too, so
    # the refusal is what shows it fired and was turned away.
    assert executed is False
    (strict_timer,) = svc.container.registry.timers
    assert strict_timer.refusal_count >= 1, "the timer never fired, so nothing was refused"
    assert strict_timer.last_refusal == "unauthenticated", strict_timer.last_refusal
    assert strict_timer.error_count == 0, strict_timer.last_error


@pytest.mark.asyncio
async def test_timer_with_default_timer_user_populates_context_and_roles(auth_service):
    """default_timer_user populates AuthContext allowing @requires_roles to pass."""
    bot_user = AuthUser(
        user_id="bot-1",
        username="system-cron",
        email="cron@internal",
        roles={"internal_cron", "admin"},
    )

    seen_context: AuthContext | None = None

    class RoleGuardedService(CliffracerService):
        auth = AuthExtension(auth_service, default_timer_user=bot_user)

        @timer(interval=0.01)
        @requires_roles("internal_cron")
        async def on_tick(self):
            nonlocal seen_context
            seen_context = get_current_context()

    svc = RoleGuardedService(ServiceConfig(name="role-svc"))
    await _one_interval(svc)

    assert seen_context is not None
    assert seen_context.user.username == "system-cron"
    assert "internal_cron" in seen_context.user.roles
    assert seen_context.is_valid is True


@pytest.mark.asyncio
async def test_timer_with_default_timer_user_fails_missing_role(auth_service):
    """default_timer_user without required role raises AuthorizationError."""
    bot_user = AuthUser(
        user_id="bot-2",
        username="limited-bot",
        email="limited@internal",
        roles={"worker"},
    )
    executed = False

    class RoleGuardedService(CliffracerService):
        auth = AuthExtension(auth_service, default_timer_user=bot_user)

        @timer(interval=0.01)
        @requires_roles("superuser")
        async def on_tick(self):
            nonlocal executed
            executed = True

    svc = RoleGuardedService(ServiceConfig(name="role-svc-fail"))
    await _one_interval(svc)

    assert executed is False
    # AuthorizationError: the timer identity existed and lacked the role, and the decorator raised
    # inside the handler, so it is an error. A missing identity also leaves the handler unrun, but
    # `worker_setup` refuses it with `RejectMessage`, which the timer counts as a refusal.
    (guarded_timer,) = svc.container.registry.timers
    assert guarded_timer.error_count >= 1, "the timer never fired, so nothing was refused"
    assert (guarded_timer.last_error or "").startswith("AuthorizationError"), (
        guarded_timer.last_error
    )


@pytest.mark.asyncio
async def test_timer_with_static_headers(auth_service):
    """Timer configured with headers={"authorization": ...} authenticates against AuthExtension."""
    token = auth_service.authenticate("timer_runner", "password123")
    assert token is not None

    seen_context: AuthContext | None = None

    class HeaderTimerService(CliffracerService):
        auth = AuthExtension(auth_service)

        @timer(interval=0.01, headers={"authorization": f"Bearer {token}"})
        @requires_roles("scheduler")
        async def scheduled_work(self):
            nonlocal seen_context
            seen_context = get_current_context()

    svc = HeaderTimerService(ServiceConfig(name="header-timer-svc"))
    await _one_interval(svc)

    assert seen_context is not None
    assert seen_context.user.username == "timer_runner"
    assert "scheduler" in seen_context.user.roles


@pytest.mark.asyncio
async def test_timer_with_token_factory(auth_service):
    """Timer configured with token_factory generates token dynamically for AuthExtension."""
    seen_context: AuthContext | None = None

    def dynamic_token() -> str:
        tok = auth_service.authenticate("timer_runner", "password123")
        return f"Bearer {tok}"

    class TokenFactoryService(CliffracerService):
        auth = AuthExtension(auth_service)

        @timer(interval=0.01, token_factory=dynamic_token)
        @requires_roles("worker")
        async def work_with_token(self):
            nonlocal seen_context
            seen_context = get_current_context()

    svc = TokenFactoryService(ServiceConfig(name="factory-timer-svc"))
    await _one_interval(svc)

    assert seen_context is not None
    assert seen_context.user.username == "timer_runner"
    assert "worker" in seen_context.user.roles
