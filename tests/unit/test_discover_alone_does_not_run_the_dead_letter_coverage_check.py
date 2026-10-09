"""`HandlerDiscovery.discover` runs the four listener validations, and not the dead-letter coverage check.

`validate_dlq_coverage` needs the declared streams, so the container runs it at
startup (`_assert_dlq_covered`). A caller of `discover` alone, which is how much
of the unit suite builds a registry, has not had it: the docstring says so, and
this pins both halves, so a change that moves the check into `discover` has to
update the docstring with it.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.jetstream import StreamDeclarationError, StreamSpec

pytestmark = pytest.mark.unit


class Svc(CliffracerService):
    @listener("orders.created", fanout=True)
    async def on_order(self, subject: str) -> None:
        pass


class NoDeclaration(CliffracerService):
    @listener("orders.created")
    async def on_order(self, subject: str) -> None:
        pass


def _uncovered() -> ServiceConfig:
    """JetStream on, one stream, and nothing that covers `dlq.orders_svc`."""
    return ServiceConfig(
        name="orders_svc",
        jetstream_enabled=True,
        jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.*"])],
    )


def test_discover_returns_a_registry_for_a_config_whose_dead_letter_subject_is_uncovered():
    config = _uncovered()

    registry = HandlerDiscovery.discover(Svc(config), config)

    assert "orders.created" in registry.event_fanout


def test_the_coverage_check_is_a_separate_call_and_refuses_that_config():
    with pytest.raises(StreamDeclarationError, match="dead-letter"):
        HandlerDiscovery.validate_dlq_coverage(_uncovered())


def test_CONTROL_discover_does_run_the_listener_validations():
    """Without this, the first test could pass because `discover` validates nothing."""
    config = _uncovered()

    with pytest.raises(ConfigurationError, match="neither a durable nor fanout"):
        HandlerDiscovery.discover(NoDeclaration(config), config)
