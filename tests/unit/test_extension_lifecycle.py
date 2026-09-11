import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import Extension, entrypoint
from tests.conftest import declared


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


@pytest.mark.unit
def test_extensions_are_bound_per_instance_in_declaration_order():
    a = Svc(ServiceConfig(name="a"))
    b = Svc(ServiceConfig(name="b"))
    assert declared(a) == ["first", "second"]
    assert a.first is not Svc.first and a.first is not b.first
    assert a.first.service is a


@pytest.mark.unit
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


@pytest.mark.unit
async def test_health_and_info_collect_contributions_under_the_extension_name(monkeypatch):
    svc = Svc(ServiceConfig(name="a"))
    health = await svc.health_check()
    assert health["first"] == {"tag": "first"}
    assert health["second"] == {"tag": "second"}
    assert svc.get_service_info()["first"] == {"tag": "first"}


@pytest.mark.unit
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
    assert health["bad"] == {"error": "RuntimeError: boom"}
    assert health["status"] == "stopped"


@pytest.mark.unit
def test_add_extension_binds_and_appends():
    svc = Svc(ServiceConfig(name="a"))
    third = svc.add_extension(Rec("third"), name="third")
    assert third.service is svc
    assert declared(svc) == ["first", "second", "third"]


@pytest.mark.unit
def test_an_unknown_entrypoint_kind_is_an_error_at_discovery():
    class Owner(Extension):
        pass

    class S(CliffracerService):
        owner = Owner()

        @entrypoint("nope", owner=Owner())
        async def handler(self):
            pass

    svc = S(ServiceConfig(name="s"))
    with pytest.raises(TypeError, match="no extension registers entrypoint kind 'nope'"):
        svc._discover_handlers()


@pytest.mark.unit
def test_a_registered_kind_is_bound_through_its_owner():
    seen = []

    class Owner(Extension):
        def entrypoint_kinds(self):
            return {"route": self._bind}

        def _bind(self, service, method_name, bound, spec):
            seen.append((self.name, method_name, spec))

    owner = Owner()

    class S(CliffracerService):
        http = owner

        @entrypoint("route", owner=owner, path="/x")
        async def handler(self):
            pass

    svc = S(ServiceConfig(name="s"))
    svc._discover_handlers()
    assert seen == [("http", "handler", {"path": "/x"})]


class _FakeNC:
    is_closed = False


async def _noop(*a, **k):
    return None
