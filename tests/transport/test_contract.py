"""The shared transport contract, run against the in-memory transport.

The same cases run against a real `nats-py` client in
`tests/integration/test_transport_contract.py`. Both legs read `CASES` from
`tests/contract/transport_cases.py`, so neither can gain or lose a case on its
own -- which is what makes agreement between them mean anything.
"""

import uuid
from typing import Any

import pytest

from tests.contract.transport_cases import CASES, Backend, Case

from .conftest import Connection

pytestmark = pytest.mark.unit


@pytest.mark.parametrize("case", CASES, ids=lambda c: c.name)
async def test_the_in_memory_transport_keeps_the_contract(
    case: Case, mock_transport: Connection
) -> None:
    """Every contract case holds for the in-memory broker."""
    others: list[Connection] = []

    async def connect(**options: Any) -> Connection:
        others.append(await mock_transport.broker.connect(**options))
        return others[-1]

    backend = Backend(
        name="memory",
        client=mock_transport,
        subject_prefix=f"contract-memory-{uuid.uuid4().hex[:8]}",
        # The broker answers a request with no listener directly, so the feature
        # the real client needs its server to advertise is always available here.
        supports_no_responders=True,
        connect=connect,
    )
    try:
        await case.run(backend)
    finally:
        for conn in others:
            await conn.close()
