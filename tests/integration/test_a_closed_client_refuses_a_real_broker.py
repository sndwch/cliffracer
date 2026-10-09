"""The close-then-reuse refusal, against a live broker.

The unit tier patches `nats.connect`, so every assertion there would hold if
the client had stopped being able to talk to a broker at all. This says the
refusal is the client's decision rather than a side effect of a connection that
was going to fail anyway: the same client reaches a real broker first.
"""

import os

import pytest

from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcConnectionError

pytestmark = pytest.mark.integration

BROKER_ENV = "CLIFFRACER_TEST_NATS_URL"


@pytest.mark.nats_required
async def test_a_real_client_works_then_refuses_once_it_is_closed() -> None:
    """Connected, drained, and then refusing by name -- not a raw nats error."""
    url = os.getenv(BROKER_ENV)
    if not url:
        pytest.skip(f"${BROKER_ENV} is not set; there is no broker to check against")

    client = ServiceClient(service="closed_probe", nats_url=url, verify=False, timeout=1.0)
    nc = await client._connection()
    assert nc.is_connected, "the control half: it reached the broker before being closed"

    await client.close()

    with pytest.raises(RpcConnectionError) as caught:
        await client._connection()
    assert "closed" in str(caught.value), str(caught.value)
