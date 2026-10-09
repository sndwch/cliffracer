"""A module that asks for a broker gets the prefix; an in-memory one does not.

The prefix is applied per module rather than per session, because
`ServiceConfig` reads it at construction and exporting it for the whole run
reached the in-memory tiers too -- where a test hand-delivers a message built
from a bare subject literal while the container subscribes under the prefix.

That scoping can fail in two directions, and only one of them is loud. Applying
it too widely breaks the in-memory tiers, which is visible. **Applying it too
narrowly is silent**: the broker-backed tiers simply stop being isolated, go on
passing, and collide with another run days later. So this asserts the prefix is
in effect HERE, in a module that carries `nats_required`, rather than inferring
it from a tier that went green.

Its opposite number is `tests/unit/test_an_in_memory_module_is_not_prefixed.py`.
The pair is the point: neither alone says the scoping is right.
"""

import os

import pytest

from tests.broker_isolation import PREFIX_ENV

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


def test_this_module_carries_the_prefix_when_the_session_has_one(_broker_namespace):
    """Takes the session fixture, rather than re-deriving its decision.

    `isolation_requested()` reads the two flags; it does not count a caller who
    exported `CLIFFRACER_SUBJECT_PREFIX` directly, which the session fixture
    honours as well. Asking the fixture what it decided removes that second
    definition -- the first draft of this test re-derived it and then failed on
    the caller-supplied path, which is the disagreement it now cannot have.
    """
    prefix = os.environ.get(PREFIX_ENV)
    if _broker_namespace is None:
        assert not prefix, (
            f"the session is not isolating, so nothing should have set {PREFIX_ENV}, "
            f"but it is {prefix!r}"
        )
        return

    assert prefix, (
        f"{PREFIX_ENV} is unset in a module marked nats_required while the session "
        f"holds the prefix {_broker_namespace!r}, so this tier is sharing the "
        "broker's unprefixed names with every other run -- which fails silently"
    )
    assert prefix.startswith(_broker_namespace), (
        f"{prefix!r} does not begin with the session's {_broker_namespace!r}, so "
        "the teardown sweep will not match what this module created"
    )
    assert prefix != _broker_namespace, (
        "the module token was not appended, so two modules in this run share one "
        "prefix and can claim each other's subjects"
    )
