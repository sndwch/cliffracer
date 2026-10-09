"""A listener declared with `pause_when_down` is paused and resumed by a background probe.

What is pinned here is the decision: which dependencies are probed, how many failed or passing
probes change a dependency's state, which listeners that pauses and resumes, and what is said while
one stays paused. `ListenerPauses` is driven a round at a time through `tick()`, with the container's
pause and resume replaced by recorders. That a pause drops the replica's interest in a durable,
spends no delivery attempt and rebinds it on resume is checked against a broker in
`tests/integration/test_a_listener_paused_on_a_dependency_spends_no_delivery_attempt.py`.
"""

import asyncio
from types import SimpleNamespace

import pytest

from cliffracer import CliffracerService, ServiceConfig, StreamSpec, dependency, listener
from cliffracer.core.dependencies import Dependency
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.listener_pause import WARN_EVERY, ListenerPauses
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit


class Probe:
    """A dependency probe whose answer the test sets, counting how often it is asked."""

    def __init__(self) -> None:
        self.up = True
        self.calls = 0

    async def __call__(self) -> None:
        self.calls += 1
        if not self.up:
            raise ConnectionError("refused")


class Lines:
    """A logger that keeps what was said, by level."""

    def __init__(self) -> None:
        self.said: list[tuple[str, str]] = []

    def info(self, text: str) -> None:
        self.said.append(("info", text))

    def warning(self, text: str) -> None:
        self.said.append(("warning", text))

    def error(self, text: str) -> None:
        self.said.append(("error", text))


def _pauses(listeners, probes, *, pause_after=2, resume_after=2, streams=(), fail_pause=None):
    config = SimpleNamespace(
        dependency_probe_interval=0.01,
        dependency_pause_after=pause_after,
        dependency_resume_after=resume_after,
    )
    events: list[tuple[str, str]] = []
    hooks: list[tuple[str, str, tuple[str, ...]]] = []

    async def pause(subject):
        if fail_pause is not None and fail_pause():
            raise RuntimeError("the broker refused the unsubscribe")
        events.append(("pause", subject))

    async def resume(subject):
        events.append(("resume", subject))

    async def notify(hook, subject, dependencies):
        hooks.append((hook, subject, dependencies))

    deps = [Dependency(name=name, probe=probe) for name, probe in probes.items()]
    log = Lines()
    pauses = ListenerPauses(
        listeners=listeners,
        dependencies=lambda: deps,
        config=config,
        pause=pause,
        resume=resume,
        logger=log,
        notify=notify,
        streams=list(streams),
    )
    return pauses, events, hooks, log


async def test_two_failed_probes_pause_and_two_passing_ones_resume():
    db = Probe()
    pauses, events, hooks, _ = _pauses({"orders.created": ("db",)}, {"db": db})

    db.up = False
    await pauses.tick()
    assert events == [], "one failed probe paused the listener"
    await pauses.tick()
    assert events == [("pause", "orders.created")]
    assert list(pauses.paused) == ["orders.created"]
    assert pauses.paused["orders.created"]["dependencies"] == ["db"]

    db.up = True
    await pauses.tick()
    assert events == [("pause", "orders.created")], "one passing probe resumed the listener"
    await pauses.tick()
    assert events == [("pause", "orders.created"), ("resume", "orders.created")]
    assert pauses.paused == {}
    assert hooks == [
        ("on_listener_paused", "orders.created", ("db",)),
        ("on_listener_resumed", "orders.created", ("db",)),
    ]


async def test_a_dependency_that_alternates_never_pauses_its_listener():
    """A failure followed by a pass resets the count, so a flapping probe does not pause."""
    db = Probe()
    pauses, events, _, _ = _pauses({"orders.created": ("db",)}, {"db": db})

    for round_ in range(12):
        db.up = round_ % 2 == 1
        await pauses.tick()

    assert events == []


async def test_the_counts_are_the_configured_ones():
    db = Probe()
    pauses, events, _, _ = _pauses(
        {"orders.created": ("db",)}, {"db": db}, pause_after=3, resume_after=1
    )

    db.up = False
    for _ in range(2):
        await pauses.tick()
    assert events == []
    await pauses.tick()
    assert events == [("pause", "orders.created")]
    db.up = True
    await pauses.tick()
    assert events == [("pause", "orders.created"), ("resume", "orders.created")]


async def test_a_listener_naming_two_dependencies_waits_for_both():
    db, cache = Probe(), Probe()
    pauses, events, _, _ = _pauses(
        {"orders.created": ("db", "cache")},
        {"db": db, "cache": cache},
        pause_after=1,
        resume_after=1,
    )

    cache.up = False
    await pauses.tick()
    assert events == [("pause", "orders.created")]
    assert pauses.paused["orders.created"]["dependencies"] == ["cache"]

    db.up = False
    cache.up = True
    await pauses.tick()
    assert events == [("pause", "orders.created")], "it resumed with db still down"
    assert pauses.paused["orders.created"]["dependencies"] == ["db"]

    db.up = True
    await pauses.tick()
    assert events[-1] == ("resume", "orders.created")


async def test_only_the_listeners_that_name_a_down_dependency_pause():
    db, cache = Probe(), Probe()
    pauses, events, _, _ = _pauses(
        {"orders.created": ("db",), "carts.updated": ("cache",)},
        {"db": db, "cache": cache},
        pause_after=1,
    )

    db.up = False
    await pauses.tick()

    assert events == [("pause", "orders.created")]


async def test_only_the_dependencies_a_listener_names_are_probed():
    db, unrelated = Probe(), Probe()
    pauses, _, _, _ = _pauses({"orders.created": ("db",)}, {"db": db, "other": unrelated})

    for _ in range(3):
        await pauses.tick()

    assert (db.calls, unrelated.calls) == (3, 0)


async def test_a_pause_that_fails_is_tried_again_on_the_next_round():
    db = Probe()
    refusing = [True]
    pauses, events, hooks, log = _pauses(
        {"orders.created": ("db",)}, {"db": db}, pause_after=1, fail_pause=lambda: refusing[0]
    )

    db.up = False
    await pauses.tick()
    assert (events, pauses.paused, hooks) == ([], {}, [])
    assert any(level == "error" and "could not pause" in text for level, text in log.said)

    refusing[0] = False
    await pauses.tick()
    assert events == [("pause", "orders.created")]


async def test_a_listener_still_paused_is_named_every_tenth_round_with_its_stream_limits():
    db = Probe()
    streams = [StreamSpec(name="ORDERS", subjects=["orders.>"], max_age_seconds=3600)]
    pauses, _, _, log = _pauses(
        {"orders.created": ("db",)}, {"db": db}, pause_after=1, streams=streams
    )

    db.up = False
    for _ in range(WARN_EVERY * 2):
        await pauses.tick()

    still = [text for level, text in log.said if level == "warning" and "still paused" in text]
    assert len(still) == 2, still
    assert "'orders.created'" in still[0] and "['db']" in still[0]
    assert "ORDERS max_age_seconds=3600" in still[0]


async def test_a_paused_listener_without_age_limits_says_its_messages_wait():
    db = Probe()
    streams = [StreamSpec(name="ORDERS", subjects=["orders.>"])]
    pauses, _, _, log = _pauses(
        {"orders.created": ("db",)}, {"db": db}, pause_after=1, streams=streams
    )

    db.up = False
    for _ in range(WARN_EVERY):
        await pauses.tick()

    (still,) = [text for level, text in log.said if "still paused" in text]
    assert "ORDERS sets no max age, so its messages wait" in still


async def test_nothing_resumes_a_listener_whose_dependency_never_comes_back():
    db = Probe()
    pauses, events, _, _ = _pauses({"orders.created": ("db",)}, {"db": db}, pause_after=1)

    db.up = False
    for _ in range(WARN_EVERY * 3):
        await pauses.tick()

    assert events == [("pause", "orders.created")]
    assert list(pauses.paused) == ["orders.created"]


async def test_the_loop_runs_a_round_per_interval_until_the_service_stops():
    db = Probe()
    pauses, events, _, _ = _pauses({"orders.created": ("db",)}, {"db": db})
    running = [True]
    db.up = False

    loop = asyncio.create_task(pauses.run(lambda: running[0]))
    try:
        for _ in range(500):
            if events:
                break
            await asyncio.sleep(0.01)
    finally:
        running[0] = False
        await asyncio.wait_for(loop, 5)

    assert events == [("pause", "orders.created")]
    assert db.calls >= 2


# --- Refused at declaration and at discovery ------------------------------------------------


def test_a_bare_string_is_refused_rather_than_read_as_one_name_per_letter():
    with pytest.raises(ConfigurationError, match=r"pause_when_down='db', a str"):

        @listener("orders.created", durable="w", pause_when_down="db")  # type: ignore[arg-type]
        async def on_order(self, subject: str) -> None: ...


def test_a_name_given_twice_is_refused():
    with pytest.raises(ConfigurationError, match="names a dependency twice"):

        @listener("orders.created", durable="w", pause_when_down=("db", "db"))
        async def on_order(self, subject: str) -> None: ...


def _service_class(**listen):
    class Svc(CliffracerService):
        @dependency("db")
        async def _check_db(self) -> None: ...

        @listener("orders.created", **listen)
        async def on_order(self, subject: str) -> None: ...

    return Svc


def _discover(cls, **config):
    cfg = ServiceConfig(name="svc", health_port=0, **config)
    return HandlerDiscovery.discover(cls(cfg), cfg)


def test_a_durable_listener_naming_a_declared_dependency_is_accepted():
    reg = _discover(_service_class(durable="w", pause_when_down=("db",)), jetstream_enabled=True)

    assert reg.event_pause_when_down == {"orders.created": ("db",)}


def test_a_listener_without_a_durable_is_refused():
    with pytest.raises(ConfigurationError, match="is not a durable listener"):
        _discover(_service_class(fanout=True, pause_when_down=("db",)), jetstream_enabled=True)


def test_a_durable_on_a_service_without_jetstream_is_refused():
    with pytest.raises(ConfigurationError, match="jetstream_enabled=False"):
        _discover(_service_class(durable="w", pause_when_down=("db",)))


def test_a_name_no_dependency_carries_is_refused():
    with pytest.raises(ConfigurationError, match=r"pause_when_down on \['postgres'\]"):
        _discover(
            _service_class(durable="w", pause_when_down=("postgres",)), jetstream_enabled=True
        )


def test_a_dependency_added_before_start_counts():
    class Svc(CliffracerService):
        @listener("orders.created", durable="w", pause_when_down=("cache",))
        async def on_order(self, subject: str) -> None: ...

    cfg = ServiceConfig(name="svc", health_port=0, jetstream_enabled=True)
    svc = Svc(cfg)
    svc.add_dependency("cache", Probe())

    reg = HandlerDiscovery.discover(svc, cfg, registry=svc.container.registry)

    assert reg.event_pause_when_down == {"orders.created": ("cache",)}


def test_describe_refuses_a_pause_on_a_listener_without_a_durable():
    with pytest.raises(ConfigurationError, match="is not a durable listener"):
        describe(_service_class(fanout=True, pause_when_down=("db",)))
