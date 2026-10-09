"""The timeout message carries the number the probe wait was actually given.

`_run_one` read `dep.timeout` twice: once to bound the probe and once to render
`timed out after {…}s`. `Dependency` is frozen, so those two reads agree today
and the message is not wrong -- the problem is that it CANNOT be wrong for the
right reason. The assertion that checked it compared the message against the
same attribute that produced it, so it agreed with itself whatever the wait
received, and a defect that handed the wait a different value would have kept
the message looking honest.

READS THE DECISION, NOT THE ARTIFACT. These record the timeout `asyncio.wait`
(the call that waits on the probe's task) is actually called with and require the message to name THAT. A frozen
dataclass cannot be made to disagree with itself, so the disagreement has to be
introduced where the bound is passed, which is the only place it could ever
come from.
"""

from __future__ import annotations

import asyncio

import pytest

from cliffracer.core.dependencies import Dependency, _run_one

pytestmark = pytest.mark.unit


async def _hang() -> None:
    await asyncio.sleep(3600)


def _record_wait(monkeypatch: pytest.MonkeyPatch) -> dict[str, float]:
    """Capture the timeout `_run_one` hands to `asyncio.wait`."""
    seen: dict[str, float] = {}
    real = asyncio.wait

    async def recording(fs, *, timeout=None, **kwargs):  # type: ignore[no-untyped-def]
        if timeout is not None:
            seen["timeout"] = timeout
        return await real(fs, timeout=timeout, **kwargs)

    monkeypatch.setattr(asyncio, "wait", recording)
    return seen


@pytest.mark.asyncio
async def test_the_message_names_the_timeout_the_wait_received(monkeypatch):
    """The load-bearing one: message against the recorded bound, not the config."""
    seen = _record_wait(monkeypatch)
    dep = Dependency(name="slow", probe=_hang, timeout=0.05)

    result = await _run_one(dep)

    assert result["ok"] is False
    assert "timeout" in seen, "the wait was never given a timeout, so this checked nothing"
    assert f"{seen['timeout']}" in result["error"], (
        f"the message says {result['error']!r} but the wait was given "
        f"{seen['timeout']}; the message is rendered from something other than "
        "the bound that fired"
    )


@pytest.mark.asyncio
async def test_CONTROL_the_recorder_sees_the_configured_value(monkeypatch):
    """Pins what the test above is comparing against.

    If the recorder captured nothing, or captured something unrelated to the
    dependency, the comparison would still pass whenever the message happened
    to contain that string.
    """
    seen = _record_wait(monkeypatch)
    dep = Dependency(name="slow", probe=_hang, timeout=0.05)

    await _run_one(dep)

    assert seen["timeout"] == 0.05, seen


@pytest.mark.asyncio
async def test_CONTROL_an_ordinary_timeout_still_renders_its_value():
    """Unpatched, so the message is the one an operator actually reads."""
    result = await _run_one(Dependency(name="slow", probe=_hang, timeout=0.05))

    assert result["ok"] is False
    assert result["error"] == "timed out after 0.05s", result["error"]


@pytest.mark.asyncio
async def test_CONTROL_a_probe_that_returns_is_not_reported_as_timed_out():
    """A passing probe still passes, so the bound is not being applied wrongly."""

    async def quick() -> None:
        return None

    result = await _run_one(Dependency(name="quick", probe=quick, timeout=5.0))

    assert result["ok"] is True
    assert result["error"] is None
