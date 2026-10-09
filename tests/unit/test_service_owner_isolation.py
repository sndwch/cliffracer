"""A parent owner shares its supervisor only through explicit dependency wiring."""

import pytest

from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.extension import ExtensionIsolationError, SharedDependency
from cliffracer.runners import LocalSupervisor, ServiceOwner
from cliffracer.runners.contracts import ActivationUnavailable

pytestmark = pytest.mark.unit


def test_parent_specification_refuses_an_implicitly_copied_supervisor():
    supervisor = LocalSupervisor(ServiceConfig(name="shipping_host", health_port=0))

    class Orders(CliffracerService):
        children = ServiceOwner(supervisor, scope="retail")

    with pytest.raises(ExtensionIsolationError, match="SharedDependency"):
        Orders(ServiceConfig(name="orders", health_port=0))


async def test_unstarted_parent_cannot_admit_shipments():
    supervisor = LocalSupervisor(ServiceConfig(name="shipping_host", health_port=0))

    class Orders(CliffracerService):
        children = ServiceOwner(SharedDependency(supervisor), scope="retail")

    parent = Orders(ServiceConfig(name="orders", health_port=0))
    with pytest.raises(ActivationUnavailable, match="not set up"):
        _ = parent.children.owner
    with pytest.raises(ActivationUnavailable, match="closed"):
        await parent.children.ensure("shipments", "batch-a", {}, revision="warehouse-a")
