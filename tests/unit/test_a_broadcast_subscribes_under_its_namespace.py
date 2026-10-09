"""A broadcast handler subscribes to the subject `broadcast_message` publishes to.

`broadcast_message` publishes under the service's namespace and subject prefix,
and `@listener` subscribes under them. `@broadcast` and
`register_broadcast_handler` registered the raw pattern instead, so on a service
with a namespace or a prefix the handler was subscribed to a subject nothing in
that namespace publishes, and never ran. The duplicate-listener check compared
the raw pattern with a listener's prefixed subject, so the two never collided.

The subscription subject is read from the registry key the container subscribes
from, and the publish subject from a recording connection.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig, broadcast, listener
from cliffracer.core.exceptions import ConfigurationError

pytestmark = pytest.mark.unit

SUBJECT = "system.alerts"

# (config, the subject both sides must use). Each config states subject_prefix,
# so an exported $CLIFFRACER_SUBJECT_PREFIX cannot change what is asserted.
PLACES = pytest.mark.parametrize(
    ("config", "wire"),
    [
        ({"namespace": "prod", "subject_prefix": None}, "prod.system.alerts"),
        ({"subject_prefix": "ci42"}, "ci42.system.alerts"),
        ({"namespace": "prod", "subject_prefix": "ci42"}, "ci42.prod.system.alerts"),
    ],
    ids=["namespace", "subject_prefix", "both"],
)


class _Recorder:
    def __init__(self) -> None:
        self.published: list[str] = []

    async def publish(self, subject, data, headers=None, **kwargs) -> None:
        self.published.append(subject)


async def _published_subject(svc: CliffracerService) -> str:
    svc.container.nc = _Recorder()
    await svc.broadcast_message(SUBJECT, level="high")
    (subject,) = svc.container.nc.published
    return subject


class Alerts(CliffracerService):
    @broadcast(SUBJECT)
    async def on_alert(self, level: str = "") -> None: ...


@PLACES
async def test_a_broadcast_handler_subscribes_where_broadcasts_are_published(config, wire):
    svc = Alerts(ServiceConfig(name="svc", **config))
    svc._discover_handlers()
    reg = svc.container.registry

    assert await _published_subject(svc) == wire
    assert SUBJECT not in reg.event_handlers
    assert reg.event_handler_names.get(wire) == "on_alert"
    assert wire in reg.event_fanout
    assert wire in reg.broadcast_handlers
    assert wire in reg.event_specs_by_subject


@PLACES
async def test_a_registered_broadcast_handler_subscribes_where_broadcasts_are_published(
    config, wire
):
    svc = CliffracerService(ServiceConfig(name="svc", **config))

    async def handler(level: str = "") -> None: ...

    svc.register_broadcast_handler(SUBJECT, handler)
    reg = svc.container.registry

    assert await _published_subject(svc) == wire
    assert list(reg.event_handlers) == [wire]
    assert wire in reg.event_fanout
    assert wire in reg.broadcast_handlers


class ListenerOverBroadcast(CliffracerService):
    @listener(SUBJECT, fanout=True)
    async def a(self, level: str = "") -> None: ...

    @broadcast(SUBJECT)
    async def b(self, level: str = "") -> None: ...


class BroadcastOverListener(CliffracerService):
    @broadcast(SUBJECT)
    async def a(self, level: str = "") -> None: ...

    @listener(SUBJECT, fanout=True)
    async def b(self, level: str = "") -> None: ...


@pytest.mark.parametrize(
    "service_class", [ListenerOverBroadcast, BroadcastOverListener], ids=lambda c: c.__name__
)
@pytest.mark.parametrize("namespace", ["prod"])
def test_a_listener_and_a_broadcast_on_one_subject_are_refused_under_a_namespace(
    service_class, namespace
):
    """Discovery registers methods in name order, so a and b swap which registers first."""
    svc = service_class(ServiceConfig(name="svc", namespace=namespace, subject_prefix=None))

    with pytest.raises(ConfigurationError, match="Duplicate event listener"):
        svc._discover_handlers()


async def test_CONTROL_with_no_namespace_and_no_prefix_the_subject_is_the_pattern():
    svc = Alerts(ServiceConfig(name="svc", subject_prefix=None))
    svc._discover_handlers()

    assert await _published_subject(svc) == SUBJECT
    assert list(svc.container.registry.event_handlers) == [SUBJECT]


def test_CONTROL_a_listener_and_a_broadcast_on_one_subject_are_refused_without_a_namespace():
    svc = ListenerOverBroadcast(ServiceConfig(name="svc", subject_prefix=None))

    with pytest.raises(ConfigurationError, match="Duplicate event listener"):
        svc._discover_handlers()
