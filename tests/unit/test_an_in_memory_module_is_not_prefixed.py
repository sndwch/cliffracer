"""An in-memory module is left alone, whether or not isolation was asked for.

The opposite number of
`tests/integration/test_a_broker_backed_module_is_prefixed.py`. A module with no
`nats_required` test reaches no broker, so it cannot collide with another run --
and a prefix here is not merely pointless, it breaks the module: the container
subscribes under the prefix while the test hand-delivers a bare subject, and
nothing matches.

`CLIFFRACER_TEST_ISOLATE=1` over the unit tier used to give 12 failures in two
modules alone for exactly that reason.
"""

import os

import pytest

from tests.broker_isolation import PREFIX_ENV

pytestmark = pytest.mark.unit


def test_no_prefix_reaches_a_module_that_never_asks_for_a_broker():
    assert not os.environ.get(PREFIX_ENV), (
        f"{PREFIX_ENV} is set in a module with no nats_required test. A "
        "ServiceConfig built here will subscribe under the prefix while the "
        "test delivers a bare subject, and nothing will match."
    )
