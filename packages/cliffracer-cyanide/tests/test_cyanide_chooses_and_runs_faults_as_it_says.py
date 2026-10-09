"""Cyanide picks the fault its config, a handler's mode or a request header asks for, and runs it as documented."""

import dataclasses
import types
import warnings

import pytest
from cliffracer_cyanide import CyanideConfig, CyanideExtension
from cliffracer_cyanide import extension as cyanide_module
from cliffracer_cyanide.extension import Injection
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import WorkerContext

pytestmark = pytest.mark.unit


def _ctx(
    kind="rpc",
    subject="svc.rpc.work",
    headers=None,
    handler=None,
    payload=None,
    cid="c-1",
    raw=None,
):
    ctx = WorkerContext(
        kind=kind, subject=subject, headers=headers or {}, correlation_id=cid, payload=payload or {}
    )
    if handler is not None:
        ctx.data["handler_name"] = handler
    ctx.raw = raw
    return ctx


async def _service(**config):
    class Svc(CliffracerService):
        cyanide = CyanideExtension(config=CyanideConfig(**config))

    svc = Svc(ServiceConfig(name="svc", health_port=0))
    await svc.container._setup_extensions()
    return svc


@pytest.fixture
def slept(monkeypatch):
    """The sleeps cyanide asks for, recorded instead of waited for."""
    seen = []

    async def sleep(seconds):
        seen.append(seconds)

    monkeypatch.setattr(cyanide_module, "asyncio", types.SimpleNamespace(sleep=sleep))
    return seen


def _logged(level="WARNING"):
    lines = []
    sink = logger.add(lambda m: lines.append(str(m)), level=level)
    return lines, sink


async def _ok():
    return "handled"


def test_a_config_is_built_without_a_warning():
    with warnings.catch_warnings():
        warnings.simplefilter("error")
        cfg = CyanideConfig(slow_weight=0.5)
    assert isinstance(cfg, CyanideConfig)


def test_the_record_limit_defaults_to_1024():
    """The README and the environment table both give 1024."""
    assert CyanideConfig().injection_record_limit == 1024


def test_an_injection_is_frozen_and_hashable():
    record = Injection(correlation_id="c", subject="s", mode="slow", seed="x")
    with pytest.raises(dataclasses.FrozenInstanceError):
        record.mode = "drop"
    assert len({record, record}) == 1


async def test_setup_seeds_an_extension_built_without_a_seed():
    svc = await _service(enabled=True)
    assert svc.cyanide.health_details()["seed"] is not None


async def test_set_enabled_turns_faults_on(slept):
    svc = await _service(enabled=False, seed="s")
    svc.cyanide.set_enabled(True)
    await svc.cyanide.worker_setup(
        _ctx(headers={"x-cyanide-mode": "slow", "x-cyanide-delay": "0.25"}, handler="work")
    )
    assert slept == [0.25]


async def test_the_header_mode_wins_over_the_handlers(slept):
    svc = await _service(enabled=True, seed="s")
    svc.cyanide.configure_handler("work", "drop_reply")
    ctx = _ctx(headers={"x-cyanide-mode": "slow", "x-cyanide-delay": "0.25"}, handler="work")
    await svc.cyanide.worker_setup(ctx)
    assert slept == [0.25]
    assert "_cyanide_dropped_reply" not in ctx.data


async def test_a_handler_mode_applies_to_an_event_by_the_handlers_name(slept):
    """An event's subject ends in its topic, not the handler's name, so the name is what matches."""
    svc = await _service(enabled=True, seed="s")
    svc.cyanide.configure_handler("on_user_created", "slow")
    await svc.container._run_worker(
        _ctx(kind="event", subject="user.created", handler="on_user_created"), _ok
    )
    assert slept == [1.0]


async def test_a_timer_with_no_subject_runs():
    svc = await _service(enabled=False, seed="s")
    assert (
        await svc.container._run_worker(_ctx(kind="timer", subject=None, handler="tick"), _ok)
        == "handled"
    )


async def test_a_dispatch_without_a_handler_name_is_matched_by_its_subjects_last_token(slept):
    """A describe dispatch carries no handler name."""
    svc = await _service(enabled=True, seed="s")
    svc.cyanide.configure_handler("describe", "slow")
    await svc.container._run_worker(_ctx(kind="describe", subject="svc.describe"), _ok)
    assert slept == [1.0]


async def test_the_draw_does_not_depend_on_the_order_of_the_payloads_keys():
    draws = []
    for payload in ({"a": 1, "b": 2}, {"b": 2, "a": 1}):
        svc = await _service(enabled=True, seed="s", slow_weight=0.5, mode="random")
        draws.append(
            [
                svc.cyanide._compute_random_mode(
                    _ctx(subject=f"svc.rpc.w{i}", payload=payload, cid=None)
                )
                for i in range(40)
            ]
        )
    assert draws[0] == draws[1]


async def test_one_eviction_counts_one_drop(slept):
    svc = await _service(enabled=True, seed="s", injection_record_limit=1)
    for i in range(2):
        await svc.cyanide.worker_setup(
            _ctx(
                headers={"x-cyanide-mode": "slow", "x-cyanide-delay": "0"},
                handler="work",
                cid=f"c{i}",
            )
        )
    assert svc.cyanide.injections_dropped == 1


async def test_a_random_draw_of_no_fault_warns_nothing():
    svc = await _service(enabled=True, seed="s", mode="random")
    lines, sink = _logged()
    try:
        await svc.cyanide.worker_setup(_ctx(handler="work"))
    finally:
        logger.remove(sink)
    assert [line for line in lines if "cyanide" in line] == []


async def test_a_header_naming_no_mode_injects_nothing_and_is_warned_about(slept):
    svc = await _service(enabled=True, seed="s")
    ctx = _ctx(headers={"x-cyanide-mode": "bogus"}, handler="work")
    lines, sink = _logged()
    try:
        await svc.cyanide.worker_setup(ctx)
    finally:
        logger.remove(sink)
    assert "_cyanide_dropped_reply" not in ctx.data
    assert slept == []
    assert any("unrecognized cyanide mode" in line for line in lines)


@pytest.mark.timeout(10)
async def test_the_occurrence_count_is_bounded_by_the_record_limit():
    """The bound is a loop that evicts until the count fits, so a broken eviction hangs here."""
    svc = await _service(
        enabled=True, seed="s", injection_record_limit=2, slow_weight=0.5, mode="random"
    )
    for i in range(5):
        svc.cyanide._compute_random_mode(_ctx(subject=f"svc.rpc.w{i}", payload={"n": i}, cid=None))
    assert len(svc.cyanide._occurrences) == 2
