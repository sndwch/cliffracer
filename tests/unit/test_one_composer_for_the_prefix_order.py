"""The client's request lands where the service listens.

`<prefix>.<namespace>.<service>.<tail>` was composed in three places: the
service's own path through `HandlerDiscovery.with_namespace`, the describe CLI,
and `ServiceClient._subject`. They all read the same inputs and there was
nothing asserting they agreed. When they disagree the failure is an
`RpcNoRespondersError` naming a subject that reads as entirely correct, which is
what makes a disagreement expensive to diagnose.

All three now delegate to `HandlerDiscovery.scoped_subject`.

WHY AGREEMENT ALONE WOULD PROVE NOTHING. Once three call sites share one
function, asserting they agree is close to `set(x) == set(x)` -- it passes just
as happily if the shared function composes the order wrongly. So the agreement
tests below are paired with `test_the_order_is_prefix_then_namespace`, which
pins the order against a literal. The literal says the answer is right; the
agreement says nobody has quietly reintroduced a local copy. Neither is
sufficient and the pair is what this file is for.

The subscribe side is read from the subjects the container actually hands to
`nc.subscribe`, not rebuilt here, because a test that reconstructs the thing it
is checking agrees with itself by construction.
"""

from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.client import ServiceClient
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.subjects import subjects_overlap
from cliffracer.generate_client.cli import describe_subject

pytestmark = pytest.mark.unit

SERVICE = "warehouse"

SHAPES = [
    pytest.param(None, None, id="neither"),
    pytest.param("prod", None, id="namespace only"),
    pytest.param(None, "w7", id="prefix only"),
    pytest.param("prod", "w7", id="both"),
]


def _config(namespace: str | None, prefix: str | None) -> ServiceConfig:
    kwargs: dict[str, object] = {"name": SERVICE, "health_port": 0, "health_listener": False}
    if namespace is not None:
        kwargs["namespace"] = namespace
    if prefix is not None:
        kwargs["subject_prefix"] = prefix
    return ServiceConfig(**kwargs)  # type: ignore[arg-type]


async def _subscribed_subjects(config: ServiceConfig) -> list[str]:
    """Every subject the container really subscribes to, in order.

    Driven through `_setup_subscriptions` with a stub connection, so these are
    the strings the service would give the broker.
    """
    service = CliffracerService(config)
    await service.container._setup_extensions()
    service._discover_handlers()
    service.nc = AsyncMock()
    service.container.lifecycle._running = False

    await service.container._setup_subscriptions()

    return [call.args[0] for call in service.nc.subscribe.call_args_list]


# --- the order itself, against a literal -------------------------------------


def test_the_order_is_prefix_then_namespace():
    """The environment prefix is outermost. Pinned here and nowhere else.

    Without this, every agreement test in this file would pass on a composer
    that put the namespace outside the prefix.
    """
    assert (
        HandlerDiscovery.scoped_subject(
            f"{SERVICE}.rpc.ship", namespace="prod", subject_prefix="w7"
        )
        == "w7.prod.warehouse.rpc.ship"
    )


@pytest.mark.parametrize(
    ("namespace", "prefix", "expected"),
    [
        (None, None, "warehouse.rpc.ship"),
        ("prod", None, "prod.warehouse.rpc.ship"),
        (None, "w7", "w7.warehouse.rpc.ship"),
        ("prod", "w7", "w7.prod.warehouse.rpc.ship"),
    ],
)
def test_each_absent_part_drops_its_dot(namespace, prefix, expected):
    """An absent namespace or prefix leaves no empty token behind.

    `w7..warehouse.rpc.ship` is a different subject and NATS will happily
    subscribe to it, so this is a real failure mode rather than cosmetics.
    """
    assert (
        HandlerDiscovery.scoped_subject(
            f"{SERVICE}.rpc.ship", namespace=namespace, subject_prefix=prefix
        )
        == expected
    )


# --- the client reaches the service ------------------------------------------


@pytest.mark.parametrize(("namespace", "prefix"), SHAPES)
@pytest.mark.asyncio
async def test_the_clients_describe_subject_is_one_the_service_subscribes_to(
    namespace, prefix, monkeypatch
):
    """Equality against the real subscribe side, not against a rebuilt string."""
    if prefix is None:
        monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)
    else:
        monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", prefix)

    subscribed = await _subscribed_subjects(_config(namespace, prefix))
    client_side = ServiceClient(service=SERVICE, namespace=namespace)._subject("describe")

    assert client_side in subscribed, (
        f"the client would ask on {client_side!r}, which is not among the subjects the "
        f"service subscribes to: {subscribed}. A client that cannot reach its own "
        f"service fails with RpcNoRespondersError naming a subject that looks correct."
    )


@pytest.mark.parametrize(("namespace", "prefix"), SHAPES)
@pytest.mark.asyncio
async def test_the_clients_rpc_subject_is_covered_by_what_the_service_listens_on(
    namespace, prefix, monkeypatch
):
    """The RPC subscription is a wildcard, so this is coverage, not equality.

    Uses the framework's own matcher rather than a string comparison, so the
    test agrees with NATS about what `rpc.*` covers.
    """
    if prefix is None:
        monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)
    else:
        monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", prefix)

    subscribed = await _subscribed_subjects(_config(namespace, prefix))
    client_side = ServiceClient(service=SERVICE, namespace=namespace)._subject("rpc.ship")

    assert any(subjects_overlap(pattern, client_side) for pattern in subscribed), (
        f"nothing the service subscribes to covers {client_side!r}: {subscribed}"
    )


@pytest.mark.parametrize(("namespace", "prefix"), SHAPES)
def test_the_describe_cli_and_the_client_agree(namespace, prefix, monkeypatch):
    """The two config-less paths, which were the two copies that drifted."""
    if prefix is None:
        monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)
    else:
        monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", prefix)

    from_cli = describe_subject(SERVICE, namespace)
    from_client = ServiceClient(service=SERVICE, namespace=namespace)._subject("describe")

    assert from_cli == from_client


@pytest.mark.parametrize(("namespace", "prefix"), SHAPES)
def test_the_config_path_and_the_environment_path_agree(namespace, prefix, monkeypatch):
    """A service holding a config, against a client reading the variable.

    These are the two halves that cannot see each other at runtime: the service
    knows its `ServiceConfig`, the generated client knows only the environment.
    """
    if prefix is None:
        monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)
    else:
        monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", prefix)

    from_config = HandlerDiscovery.with_namespace(_config(namespace, prefix), f"{SERVICE}.rpc.ship")
    from_client = ServiceClient(service=SERVICE, namespace=namespace)._subject("rpc.ship")

    assert from_config == from_client


# --- controls ----------------------------------------------------------------


def test_CONTROL_without_a_prefix_the_client_is_byte_identical_to_before(monkeypatch):
    """The change must not move any subject for anyone not using the field."""
    monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)

    assert ServiceClient(service=SERVICE)._subject("describe") == "warehouse.describe"
    assert (
        ServiceClient(service=SERVICE, namespace="prod")._subject("rpc.ship")
        == "prod.warehouse.rpc.ship"
    )


def test_CONTROL_an_empty_variable_reads_as_unset(monkeypatch):
    """`CLIFFRACER_SUBJECT_PREFIX=` must not prepend an empty token."""
    monkeypatch.setenv("CLIFFRACER_SUBJECT_PREFIX", "")

    assert ServiceClient(service=SERVICE)._subject("describe") == "warehouse.describe"


@pytest.mark.asyncio
async def test_CONTROL_the_subscribed_list_is_not_empty():
    """The agreement tests above would pass vacuously against an empty list.

    `in subscribed` and `any(...)` are both true-by-default-free only if there
    is something to be in, so this pins that the stub actually recorded the
    service's subscriptions.
    """
    subscribed = await _subscribed_subjects(_config("prod", "w7"))

    assert len(subscribed) >= 3, subscribed
    assert all(s.startswith("w7.prod.") for s in subscribed), subscribed
