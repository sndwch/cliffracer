"""Unit tests for the @cron decorator and CronTimer."""

import asyncio
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest
from cliffracer_cron import CronTimer, cron

pytestmark = pytest.mark.unit

UTC = ZoneInfo("UTC")


class TestCronDecorator:
    def test_decorator_registers_cron_timer(self):
        """@cron marks the method with a CronTimer in _cliffracer_timers."""

        @cron("0 9 * * *")
        async def daily_report(self):
            pass

        assert hasattr(daily_report, "_cliffracer_timers")
        assert len(daily_report._cliffracer_timers) == 1
        timer = daily_report._cliffracer_timers[0]
        assert isinstance(timer, CronTimer)
        assert timer.method_name == "daily_report"
        assert timer.expression == "0 9 * * *"

    def test_invalid_expression_raises_at_decoration(self):
        """A bad cron string fails fast when the decorator is applied."""
        with pytest.raises(ValueError, match="cron expression"):

            @cron("not a cron expression")
            async def bad(self):
                pass

    def test_invalid_timezone_raises(self):
        """An unknown timezone fails fast."""
        with pytest.raises(ValueError, match="timezone"):

            @cron("0 9 * * *", tz="Mars/Phobos")
            async def bad_tz(self):
                pass

    def test_named_schedule_is_accepted(self):
        """croniter's named schedules like @hourly are valid."""

        @cron("@hourly")
        async def hourly(self):
            pass

        assert hourly._cliffracer_timers[0].expression == "@hourly"


class TestCronSchedule:
    def test_seconds_until_next_utc(self):
        """0 9 * * * one hour before 9am UTC -> 3600s."""
        timer = CronTimer("0 9 * * *", tz="UTC")
        now = datetime(2026, 1, 1, 8, 0, tzinfo=UTC)
        assert timer._seconds_until_next(now) == pytest.approx(3600.0, abs=1.0)

    def test_seconds_until_next_respects_timezone(self):
        """0 9 * * * in Chicago, one hour before 9am Chicago -> 3600s."""
        chicago = ZoneInfo("America/Chicago")
        timer = CronTimer("0 9 * * *", tz="America/Chicago")
        now = datetime(2026, 1, 1, 8, 0, tzinfo=chicago)
        assert timer._seconds_until_next(now) == pytest.approx(3600.0, abs=1.0)

    def test_next_is_strictly_future(self):
        """Exactly on a boundary returns the NEXT occurrence, not now (no double-fire)."""
        timer = CronTimer("0 9 * * *", tz="UTC")
        on_boundary = datetime(2026, 1, 1, 9, 0, tzinfo=UTC)
        # next should be ~24h away, never 0
        assert timer._seconds_until_next(on_boundary) == pytest.approx(86400.0, abs=1.0)


class TestCronExecution:
    @pytest.mark.asyncio
    async def test_eager_fires_immediately(self):
        """eager=True runs the method once on start, without waiting for the schedule."""

        class FakeService:
            def __init__(self):
                self.calls = 0

            async def job(self):
                self.calls += 1

        svc = FakeService()
        timer = CronTimer("0 0 1 1 *", tz="UTC", eager=True)  # Jan 1 — won't fire during the test
        timer.method_name = "job"
        await timer.start(svc)
        await asyncio.sleep(0.05)
        await timer.stop()
        assert svc.calls == 1

    @pytest.mark.asyncio
    async def test_not_eager_does_not_fire_immediately(self):
        """Without eager, a far-future schedule does not fire during the test window."""

        class FakeService:
            def __init__(self):
                self.calls = 0

            async def job(self):
                self.calls += 1

        svc = FakeService()
        timer = CronTimer("0 0 1 1 *", tz="UTC", eager=False)
        timer.method_name = "job"
        await timer.start(svc)
        await asyncio.sleep(0.05)
        await timer.stop()
        assert svc.calls == 0

    def test_get_stats_reports_expression(self):
        timer = CronTimer("*/5 * * * *", tz="UTC")
        timer.method_name = "job"
        stats = timer.get_stats()
        assert stats["expression"] == "*/5 * * * *"
        assert stats["tz"] == "UTC"


class TestCronDiscovery:
    def test_cron_handler_discovered_as_timer(self):
        """A @cron method on a real service is picked up by handler discovery,
        proving it reuses the existing _cliffracer_timers machinery."""
        from cliffracer import CliffracerService, ServiceConfig

        class CronService(CliffracerService):
            @cron("0 9 * * *")
            async def daily(self):
                pass

        svc = CronService(ServiceConfig(name="cron_discovery_svc"))
        svc._discover_handlers()

        cron_timers = [t for t in svc._timers if isinstance(t, CronTimer)]
        assert len(cron_timers) == 1
        assert cron_timers[0].method_name == "daily"

    def test_crontimer_clone(self):
        """CronTimer.clone() produces an independent copy preserving configuration."""

        def token_fn() -> str:
            return "tok"

        t = CronTimer(
            "*/5 * * * *",
            tz="America/Chicago",
            eager=True,
            max_drift=2.0,
            error_backoff=10.0,
            headers={"authorization": "Bearer abc"},
            token_factory=token_fn,
        )
        t.method_name = "test_cron_method"
        c = t.clone()
        assert c is not t
        assert isinstance(c, CronTimer)
        assert c.expression == "*/5 * * * *"
        assert c.tz == "America/Chicago"
        assert c.eager is True
        assert c.max_drift == 2.0
        assert c.error_backoff == 10.0
        assert c.headers == {"authorization": "Bearer abc"}
        assert c.token_factory is token_fn
        assert c.method_name == "test_cron_method"
        assert c.is_running is False
        assert c.task is None
        assert c.service_instance is None

    def test_cron_decorator_passes_headers_and_token_factory(self):
        """@cron passes headers and token_factory to CronTimer instance."""

        def token_fn() -> str:
            return "tok"

        @cron("0 * * * *", headers={"x-custom": "val"}, token_factory=token_fn)
        async def scheduled():
            pass

        timer = scheduled._cliffracer_timers[0]
        assert timer.headers == {"x-custom": "val"}
        assert timer.token_factory is token_fn
