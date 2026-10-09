"""`assert_rpc_permissions` raises whatever the interpreter's flags.

It ended in a bare `assert`. It is shipped library code, so pytest's assertion rewriting does not reach
it and `python -O` (or `PYTHONOPTIMIZE`) removes the statement: the check an application keeps in its
suite returned for a grant of nothing at all.
"""

import subprocess
import sys
import textwrap

import pytest

pytestmark = pytest.mark.unit

PROBE = textwrap.dedent(
    """
    from cliffracer import CliffracerService, ServiceConfig, rpc
    from cliffracer.broker_permissions import BrokerPermissions
    from cliffracer.testing import assert_rpc_permissions

    class Orders(CliffracerService):
        @rpc
        async def get_order(self, order_id: str) -> str:
            return order_id

    config = ServiceConfig(name="orders", health_port=0)
    grants = {GRANTS}
    try:
        assert_rpc_permissions(Orders, config, BrokerPermissions(**grants), role="client")
    except AssertionError as error:
        print("REFUSED:", error)
    else:
        print("RETURNED")
    """
)


def _run(grants: str, *flags: str) -> str:
    result = subprocess.run(
        [sys.executable, *flags, "-c", PROBE.replace("{GRANTS}", grants)],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


NOTHING = '{"publish": [], "subscribe": []}'
EVERYTHING = '{"publish": ["orders.>"], "subscribe": []}'


@pytest.mark.parametrize("flags", [(), ("-O",), ("-OO",)], ids=["plain", "-O", "-OO"])
def test_a_grant_that_omits_a_registered_call_is_refused_under_every_flag(flags):
    outcome = _run(NOTHING, *flags)

    assert outcome.startswith("REFUSED: client permissions omit registered calls"), outcome
    assert "orders.rpc.get_order" in outcome


@pytest.mark.parametrize("flags", [(), ("-O",)], ids=["plain", "-O"])
def test_CONTROL_a_grant_that_covers_every_call_returns_under_every_flag(flags):
    assert _run(EVERYTHING, *flags) == "RETURNED"
