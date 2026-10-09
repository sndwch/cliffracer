"""`stop()` pairs with `setup()`: an extension whose `setup()` was never begun is not stopped.

`docs/extensions.md` says an extension's `stop()` pairs with its `setup()`. Setup runs in
declaration order and stops at the first extension that raises, but the teardown then called
`stop()` on every extension in reverse, including those after the failure that had built nothing,
so a `stop()` written to the sentence dereferenced state `setup()` never created.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension

pytestmark = pytest.mark.unit


class Recorded(Extension):
    def __init__(self, label: str, calls: list[str], *, fails: bool = False) -> None:
        self.label = label
        self.calls = calls
        self.fails = fails

    async def setup(self, ctx) -> None:
        self.calls.append(f"{self.label}.setup")
        if self.fails:
            raise RuntimeError(f"{self.label} failed")

    async def stop(self) -> None:
        self.calls.append(f"{self.label}.stop")


def _service(calls: list[str], *, failing: str | None) -> CliffracerService:
    svc = CliffracerService(ServiceConfig(name="paired", health_port=0, subject_prefix=None))
    for label in ("first", "second", "third"):
        svc.container.extensions.append(Recorded(label, calls, fails=label == failing))
    return svc


async def test_an_extension_after_the_one_whose_setup_raised_is_not_stopped():
    calls: list[str] = []
    svc = _service(calls, failing="second")

    with pytest.raises(RuntimeError, match="second failed"):
        await svc.container._setup_extensions()
    await svc.container._stop_extensions()

    assert calls == ["first.setup", "second.setup", "second.stop", "first.stop"], calls


async def test_the_extension_whose_setup_raised_is_stopped_because_it_may_hold_part_of_it():
    calls: list[str] = []
    svc = _service(calls, failing="first")

    with pytest.raises(RuntimeError):
        await svc.container._setup_extensions()
    await svc.container._stop_extensions()

    assert calls == ["first.setup", "first.stop"], calls


async def test_CONTROL_every_extension_whose_setup_completed_is_stopped_in_reverse():
    calls: list[str] = []
    svc = _service(calls, failing=None)

    await svc.container._setup_extensions()
    await svc.container._stop_extensions()

    assert calls == [
        "first.setup",
        "second.setup",
        "third.setup",
        "third.stop",
        "second.stop",
        "first.stop",
    ]


async def test_a_service_that_never_began_its_setup_stops_no_extension():
    calls: list[str] = []
    svc = _service(calls, failing=None)

    await svc.container._stop_extensions()

    assert calls == []


async def test_CONTROL_a_second_run_sets_up_and_stops_again():
    calls: list[str] = []
    svc = _service(calls, failing=None)

    for _ in range(2):
        await svc.container._setup_extensions()
        await svc.container._stop_extensions()

    assert calls.count("first.setup") == 2 and calls.count("first.stop") == 2
