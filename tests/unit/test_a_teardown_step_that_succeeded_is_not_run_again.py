"""A failed abortive cleanup does not license a full replay of the teardown.

When startup fails, the abortive cleanup tears down what was built. If one step of it raises
(a disconnect on a socket that is already gone), the service is not marked stopped, so the stop()
a supervisor makes next enters the teardown again. It used to run EVERY step again, including
the ones that had succeeded: `stop()` on an extension that already stopped is not safe by default
(a pool closed twice, a port unbound twice), and the second pass raised a secondary error that
masked the startup failure. Only the steps that did not succeed are run again.
"""

import pytest

from cliffracer import ServiceConfig
from tests.unit.test_lifecycle_abortive_stress import InstrumentedLifecycleService

pytestmark = pytest.mark.unit


class _Svc(InstrumentedLifecycleService):
    """Startup fails after the connection is made; the first disconnect raises."""

    def __init__(self, config: ServiceConfig, *, disconnect_failures: int = 1) -> None:
        super().__init__(config)
        self.disconnect_failures_left = disconnect_failures
        self.disconnect_attempts = 0

    async def disconnect(self) -> None:
        self.disconnect_attempts += 1
        if self.disconnect_failures_left > 0:
            self.disconnect_failures_left -= 1
            raise RuntimeError("disconnect failed: the socket is already gone")
        await super().disconnect()

    async def _setup_subscriptions(self) -> None:
        raise RuntimeError("subscribe failed")


async def test_the_stop_after_a_failed_abortive_cleanup_runs_only_what_did_not_succeed():
    svc = _Svc(ServiceConfig(name="replay_svc", health_port=0))

    with pytest.raises(RuntimeError, match="subscribe failed"):
        await svc.start()
    assert svc.disconnect_attempts == 1
    assert svc.stop_timers_count == 1
    assert svc.stop_extensions_count == 1
    assert not svc.container.lifecycle.is_stopped, "the failed cleanup is not a finished teardown"

    # The second teardown: the disconnect that failed is retried (and now succeeds); the steps
    # that succeeded are not run again.
    await svc.stop()

    assert svc.disconnect_attempts == 2
    assert svc.stop_timers_count == 1, "stop_timers ran again"
    assert svc.stop_extensions_count == 1, "the extensions were stopped twice"
    assert svc.container.lifecycle.is_stopped


async def test_a_stop_that_itself_fails_still_ends_the_lifecycle_and_is_not_retried_again():
    svc = _Svc(ServiceConfig(name="persistent_svc", health_port=0), disconnect_failures=2)

    with pytest.raises(RuntimeError, match="subscribe failed"):
        await svc.start()
    with pytest.raises(RuntimeError, match="disconnect failed"):
        await svc.stop()
    assert svc.container.lifecycle.is_stopped

    await svc.stop()

    assert svc.disconnect_attempts == 2, "a further stop() after a final stop() ran the teardown"
    assert svc.stop_timers_count == 1
    assert svc.stop_extensions_count == 1


async def test_once_stopped_a_further_stop_runs_nothing_and_a_restart_runs_the_teardown_afresh():
    svc = _Svc(ServiceConfig(name="settled_svc", health_port=0), disconnect_failures=0)
    with pytest.raises(RuntimeError, match="subscribe failed"):
        await svc.start()
    assert svc.container.lifecycle.is_stopped  # the clean abortive cleanup finished the teardown
    before = (svc.stop_timers_count, svc.stop_extensions_count, svc.disconnect_attempts)

    await svc.stop()
    assert (svc.stop_timers_count, svc.stop_extensions_count, svc.disconnect_attempts) == before

    # A new start begins a new lifecycle: its teardown runs every step again.
    with pytest.raises(RuntimeError, match="subscribe failed"):
        await svc.start()
    assert svc.stop_timers_count == before[0] + 1
    assert svc.stop_extensions_count == before[1] + 1
