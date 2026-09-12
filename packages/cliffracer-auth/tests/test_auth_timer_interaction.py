"""Tests for Timer and AuthExtension interaction."""

import asyncio

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

pytestmark = pytest.mark.unit

SECRET = "test-secret-not-a-real-one-0123456789abcdef"


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
    await svc.container._setup_extensions()
    svc._discover_handlers()

    await svc.container._start_timers()
    await asyncio.sleep(0.05)
    await svc.container._stop_timers()

    assert executed is True


@pytest.mark.asyncio
async def test_timer_rejected_when_allow_timers_is_false(auth_service):
    """When allow_timers=False, timer execution without token raises AuthenticationError."""
    executed = False

    class StrictTimerService(CliffracerService):
        auth = AuthExtension(auth_service, allow_timers=False)

        @timer(interval=0.01)
        async def on_tick(self):
            nonlocal executed
            executed = True

    svc = StrictTimerService(ServiceConfig(name="strict-timer-svc"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    # When started, the timer loop will catch the error and back off
    await svc.container._start_timers()
    await asyncio.sleep(0.05)
    await svc.container._stop_timers()

    # The timer handler should NOT have executed because worker_setup raised AuthenticationError
    assert executed is False


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
    await svc.container._setup_extensions()
    svc._discover_handlers()

    await svc.container._start_timers()
    await asyncio.sleep(0.05)
    await svc.container._stop_timers()

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
    await svc.container._setup_extensions()
    svc._discover_handlers()

    await svc.container._start_timers()
    await asyncio.sleep(0.05)
    await svc.container._stop_timers()

    assert executed is False


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
    await svc.container._setup_extensions()
    svc._discover_handlers()

    await svc.container._start_timers()
    await asyncio.sleep(0.05)
    await svc.container._stop_timers()

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
    await svc.container._setup_extensions()
    svc._discover_handlers()

    await svc.container._start_timers()
    await asyncio.sleep(0.05)
    await svc.container._stop_timers()

    assert seen_context is not None
    assert seen_context.user.username == "timer_runner"
    assert "worker" in seen_context.user.roles
