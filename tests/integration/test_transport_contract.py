"""The shared transport contract, run against a real NATS client.

The in-memory leg is `tests/transport/test_contract.py`. Both legs read `CASES`
from `tests/contract/transport_cases.py`: a case added for one backend is a
case both must satisfy, which is the point of holding the list in one place.
"""

import os
import uuid
from typing import Any

import nats
import pytest

from tests.contract.transport_cases import CASES, Backend, Case

pytestmark = pytest.mark.integration

BROKER_ENV = "CLIFFRACER_TEST_NATS_URL"


@pytest.mark.nats_required
@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_a_real_client_keeps_the_contract(case: Case) -> None:
    """Every contract case holds for nats-py against a live broker."""
    url = os.getenv(BROKER_ENV)
    if not url:
        pytest.skip(f"${BROKER_ENV} is not set; there is no broker to check against")

    prefix = f"contract-nats-{uuid.uuid4().hex[:8]}"
    client = await nats.connect(url, name=prefix)
    others: list[nats.NATS] = []

    async def connect(**options: Any) -> nats.NATS:
        others.append(await nats.connect(url, name=f"{prefix}-{len(others) + 1}", **options))
        return others[-1]

    try:
        backend = Backend(
            name="nats",
            client=client,
            subject_prefix=prefix,
            # NoRespondersError rides on the no-responders protocol feature, which
            # the client enables only when the server advertises header support.
            supports_no_responders=bool(client._server_info.get("headers")),
            connect=connect,
        )
        await case.run(backend)
    finally:
        for conn in [client, *others]:
            if not conn.is_closed:
                await conn.close()
