"""`ConnectionManager.connect` hands the configured reconnect policy to nats-py.

`max_reconnect_attempts` and `reconnect_time_wait` are `ServiceConfig` fields.
Dropping either keyword from the `nats.connect` call leaves nats-py on its own
defaults (60 attempts, 2 s), so a service gives up on a broker that is away for
about two minutes. Nothing else read the keywords the manager actually sent, so
the omission went unnoticed. The policy is read from what reaches `nats.connect`.
"""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from cliffracer import ServiceConfig
from cliffracer.core.connection import ConnectionManager

pytestmark = pytest.mark.unit


async def _dial(**config: object) -> dict:
    connect = AsyncMock(return_value=MagicMock())
    manager = ConnectionManager(ServiceConfig(name="dial_policy", **config))

    with patch("cliffracer.core.dial.connect", connect):
        await manager.connect()

    return connect.await_args.kwargs


async def test_a_configured_attempt_limit_reaches_nats():
    kwargs = await _dial(max_reconnect_attempts=7)

    assert kwargs["max_reconnect_attempts"] == 7


async def test_a_configured_wait_reaches_nats():
    kwargs = await _dial(reconnect_time_wait=11)

    assert kwargs["reconnect_time_wait"] == 11


async def test_the_defaults_reconnect_forever_every_two_seconds():
    """ADR-0008: `-1`, not nats-py's default of 60 attempts."""
    kwargs = await _dial()

    assert kwargs["max_reconnect_attempts"] == -1
    assert kwargs["reconnect_time_wait"] == 2


async def test_CONTROL_the_keywords_the_other_tests_read_are_the_ones_nats_connect_receives():
    """The patch is on the name `connect()` calls: the URL and name come through it too."""
    kwargs = await _dial(nats_url="nats://example.invalid:4222")

    assert kwargs["name"] == "dial_policy"
