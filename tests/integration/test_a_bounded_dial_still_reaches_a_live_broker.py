"""The control for the bounded dial: a broker that IS there is still reached.

`ServiceClient` now bounds its first dial and asks nats-py to reconnect
forever. Every assertion about that lives in the unit tier, where the dial is
patched or aimed at an address nothing listens on -- so all of it would hold
just as well if the client had stopped being able to connect at all. This is
the test that says it can, against a real broker, with the bound in force.
"""

import os

import pytest

from cliffracer.client import ServiceClient

pytestmark = pytest.mark.integration

BROKER_ENV = "CLIFFRACER_TEST_NATS_URL"


@pytest.mark.nats_required
async def test_a_reachable_broker_is_still_connected_to_within_the_bound() -> None:
    """A live broker answers well inside a bound tight enough to catch a hang."""
    url = os.getenv(BROKER_ENV)
    if not url:
        pytest.skip(f"${BROKER_ENV} is not set; there is no broker to check against")

    client = ServiceClient(service="svc", nats_url=url, connect_timeout=5.0, verify=False)
    try:
        nc = await client._connection()
        assert nc.is_connected
    finally:
        await client.close()


@pytest.mark.nats_required
async def test_the_dial_that_reaches_a_broker_asked_to_reconnect_forever() -> None:
    """ADR-0008 holds on the connection that was actually opened, not only in the kwargs.

    The unit tier asserts `max_reconnect_attempts=-1` reached `nats.connect`.
    That is an assertion about a call; this one reads the option back off a
    connection a real broker accepted, so a value nats-py rejected or coerced
    could not pass unnoticed.
    """
    url = os.getenv(BROKER_ENV)
    if not url:
        pytest.skip(f"${BROKER_ENV} is not set; there is no broker to check against")

    client = ServiceClient(service="svc", nats_url=url, verify=False)
    try:
        nc = await client._connection()
        assert nc.options["max_reconnect_attempts"] == -1
    finally:
        await client.close()
