import json

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension
from tests.conftest import declared

pytestmark = pytest.mark.unit


class Rec(Extension):
    log: list[str]

    def __init__(self, tag: str):
        self.tag = tag

    async def setup(self, ctx):
        ctx.service.log.append(f"setup:{self.tag}")

    async def start(self):
        self.service.log.append(f"start:{self.tag}")

    async def stop(self):
        self.service.log.append(f"stop:{self.tag}")

    def health_details(self):
        return {"tag": self.tag}

    def info_details(self):
        return {"tag": self.tag}


class Svc(CliffracerService):
    first = Rec("first")
    second = Rec("second")

    def __init__(self, config):
        self.log: list[str] = []
        super().__init__(config)


def test_extensions_are_bound_per_instance_in_declaration_order():
    a = Svc(ServiceConfig(name="a"))
    b = Svc(ServiceConfig(name="b"))
    assert declared(a) == ["first", "second"]
    assert a.first is not Svc.first and a.first is not b.first
    assert a.first.service is a


async def test_setup_runs_first_in_start_then_start_and_stop_run_in_order(monkeypatch):
    """Verify setup runs at start before broker connection, followed by start and stop in order."""
    svc = Svc(ServiceConfig(name="a"))
    assert svc.log == []

    async def fake_connect():
        svc.log.append("connect")
        svc.nc = _FakeNC()

    monkeypatch.setattr(svc, "connect", fake_connect)
    monkeypatch.setattr(svc.container, "_setup_subscriptions", _noop)
    monkeypatch.setattr(svc, "disconnect", _noop)
    await svc.start()
    assert svc.log == ["setup:first", "setup:second", "connect", "start:first", "start:second"]
    await svc.stop()
    assert svc.log[5:] == ["stop:second", "stop:first"]


async def test_health_and_info_collect_contributions_under_the_extension_name(monkeypatch):
    svc = Svc(ServiceConfig(name="a"))
    health = await svc.health_check()
    assert health["first"] == {"tag": "first"}
    assert health["second"] == {"tag": "second"}
    assert svc.get_service_info()["first"] == {"tag": "first"}


async def test_a_raising_contribution_is_reported_under_its_name_and_does_not_change_status():
    class Bad(Extension):
        def health_details(self):
            raise RuntimeError("boom")

    class S(CliffracerService):
        good = Rec("good")
        bad = Bad()

    svc = S(ServiceConfig(name="s"))
    health = await svc.health_check()
    assert health["good"] == {"tag": "good"}
    # Reported under its own name, which is what this test is about. The words
    # are withheld on an unauthenticated endpoint unless the configuration says
    # otherwise; see test_the_health_endpoint_withholds_exception_text.py.
    assert health["bad"] == {"error": "health details unavailable"}
    assert "boom" not in json.dumps(health)
    assert health["status"] == "stopped"

    exposed = S(ServiceConfig(name="s", expose_internal_errors=True))
    assert (await exposed.health_check())["bad"] == {"error": "RuntimeError: boom"}


async def test_an_internal_extension_is_left_out_of_health():
    """The container's own `_correlation` and `_validation` extensions are named with a leading
    underscore, which is what keeps them out of the public payload. Neither contributes details
    today, so the filter is only observable through an internal extension that does."""
    svc = Svc(ServiceConfig(name="a"))
    svc.add_extension(Rec("hidden"), name="_hidden")

    health = await svc.health_check()

    assert "_hidden" not in health
    assert health["first"] == {"tag": "first"}


def test_an_internal_extension_is_left_out_of_info():
    svc = Svc(ServiceConfig(name="a"))
    svc.add_extension(Rec("hidden"), name="_hidden")

    info = svc.get_service_info()

    assert "_hidden" not in info
    assert info["first"] == {"tag": "first"}


def test_CONTROL_the_built_in_extensions_are_the_underscore_named_ones():
    """The premise: there are internal extensions, and they are the ones the filter is for."""
    svc = Svc(ServiceConfig(name="a"))

    internal = {e.name for e in svc.container.extensions if e.name.startswith("_")}

    assert {"_correlation", "_validation"} <= internal


def test_a_raising_info_contribution_is_logged_and_left_out_of_the_description():
    """Unlike health, which reports `{"error": ...}` under the extension's name, the description
    drops the key: a consumer reads that as an extension that contributes nothing, so the warning
    is the only record. The other extensions' keys are unaffected."""

    class Bad(Extension):
        def info_details(self):
            raise RuntimeError("boom")

    class S(CliffracerService):
        good = Rec("good")
        bad = Bad()

    svc = S(ServiceConfig(name="s"))
    lines: list[str] = []
    sink = logger.add(lambda m: lines.append(m.record["message"]), level="WARNING")
    try:
        info = svc.get_service_info()
    finally:
        logger.remove(sink)

    assert "bad" not in info
    assert info["good"] == {"tag": "good"}
    assert any("info_details of extension bad failed" in line for line in lines), lines


async def test_add_extension_after_start_is_refused(monkeypatch):
    svc = Svc(ServiceConfig(name="a"))

    async def fake_connect():
        svc.nc = _FakeNC()

    monkeypatch.setattr(svc, "connect", fake_connect)
    monkeypatch.setattr(svc.container, "_setup_subscriptions", _noop)
    monkeypatch.setattr(svc, "disconnect", _noop)
    await svc.start()
    try:
        with pytest.raises(RuntimeError, match="add_extension must be called before start"):
            svc.add_extension(Rec("late"), name="late")
    finally:
        await svc.stop()


def test_add_extension_binds_and_appends():
    svc = Svc(ServiceConfig(name="a"))
    third = svc.add_extension(Rec("third"), name="third")
    assert third.service is svc
    assert declared(svc) == ["first", "second", "third"]


class _FakeNC:
    is_closed = False


async def _noop(*a, **k):
    return None
