"""A description says which subject each listener subscribes to, as the runtime resolves it."""

import pytest
from pydantic import BaseModel

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    broadcast,
    listener,
    rpc,
    validated_listener,
)
from cliffracer.introspect import Description, describe

pytestmark = pytest.mark.unit


class Ping(BaseModel):
    n: int


class Ack(BaseModel):
    ok: bool


class LocalHub(CliffracerService):
    """Every kind of listener a service with no namespace can declare."""

    @rpc
    async def poke(self, ping: Ping) -> Ack:
        return Ack(ok=True)

    @listener("orders.created", fanout=True)
    async def on_order(self, subject: str) -> None: ...

    @validated_listener("orders.checked", Ping, fanout=True)
    async def on_checked(self, message: Ping) -> None: ...

    @broadcast("system.alerts")
    async def on_alert(self, subject: str) -> None: ...


class Hub(LocalHub):
    """Adds the cross-namespace listener, which needs a namespace to span."""

    @listener("orders.audit", fanout=True, cross_namespace=True)
    async def on_audit(self, subject: str) -> None: ...


LOCAL_DECLARED = {"orders.created", "orders.checked", "system.alerts"}
DECLARED = LOCAL_DECLARED | {"orders.audit"}

CONFIGS = [
    pytest.param({}, id="bare"),
    pytest.param({"namespace": "app1"}, id="namespace"),
    pytest.param({"namespace": "app1", "subject_prefix": "prod"}, id="namespace-and-prefix"),
    pytest.param({"subject_prefix": "prod"}, id="prefix"),
]


def _described(**config) -> tuple[Description, CliffracerService]:
    hub = Hub if config.get("namespace") else LocalHub  # a cross-namespace listener needs one
    svc = hub(ServiceConfig(name="hub", **config))
    svc._discover_handlers()
    return describe(hub, service="hub", config=svc.config), svc


def _declared(**config) -> set[str]:
    return DECLARED if config.get("namespace") else LOCAL_DECLARED


@pytest.mark.parametrize("config", CONFIGS)
def test_each_listener_names_a_subject_the_service_subscribes_to(config):
    description, svc = _described(**config)

    subscribed = set(svc.container.registry.event_handlers)

    assert {entry.effective_subject for entry in description.listeners} == subscribed
    assert len(description.listeners) == len(_declared(**config))


@pytest.mark.parametrize("config", CONFIGS)
def test_the_declared_pattern_is_still_the_pattern(config):
    description, _ = _described(**config)

    assert {entry.pattern for entry in description.listeners} == _declared(**config)


def test_a_namespaced_listener_is_not_described_by_its_bare_pattern():
    description, _ = _described(namespace="app1")

    by_pattern = {entry.pattern: entry.effective_subject for entry in description.listeners}
    assert by_pattern["orders.created"] == "app1.orders.created"
    assert by_pattern["orders.audit"] == "*.orders.audit"
    assert by_pattern["system.alerts"] == "app1.system.alerts"


def test_without_a_config_the_effective_subject_is_unknown_rather_than_the_pattern():
    description = describe(Hub, service="hub")

    assert {entry.effective_subject for entry in description.listeners} == {None}


def test_the_effective_subject_survives_the_wire_form():
    description, _ = _described(namespace="app1")

    again = Description.from_dict(description.to_dict())

    assert [e.effective_subject for e in again.listeners] == [
        e.effective_subject for e in description.listeners
    ]


def test_a_description_without_the_field_reads_as_unknown():
    wire = describe(Hub, service="hub").to_dict()
    for entry in wire["listeners"]:
        del entry["effective_subject"]

    assert {e.effective_subject for e in Description.from_dict(wire).listeners} == {None}


def test_the_description_hash_does_not_depend_on_where_listeners_subscribe():
    bare = describe(Hub, service="hub")
    scoped, _ = _described(namespace="app1", subject_prefix="prod")

    assert scoped.description_hash == bare.description_hash
    assert scoped.methods == bare.methods
