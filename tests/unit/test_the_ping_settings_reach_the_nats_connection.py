"""`ServiceConfig.ping_interval` and `max_outstanding_pings` reach `nats.connect`, and unset change nothing.

A partition that drops packets without resetting the socket is noticed only when nats-py's ping loop
gives up: it counts one outstanding ping per `ping_interval` and treats the connection as stale when
the count passes `max_outstanding_pings`. Both were fixed at nats-py's defaults (120 s and 2) because
`ServiceConfig` did not carry them, so a service could not shorten the window. They are now fields;
left unset, nothing is passed and nats-py's defaults stay, so a service that sets neither is unchanged.
"""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


async def _connect_kwargs(**config) -> dict:
    svc = CliffracerService(ServiceConfig(name="pings", health_port=0, **config))
    connect = AsyncMock(return_value=MagicMock())
    with patch("cliffracer.core.dial.connect", connect):
        await svc.container.connect()
    (call,) = connect.call_args_list
    return call.kwargs


async def test_both_settings_are_handed_to_nats_connect_as_set():
    kwargs = await _connect_kwargs(ping_interval=5.0, max_outstanding_pings=1)

    assert kwargs["ping_interval"] == 5.0
    assert kwargs["max_outstanding_pings"] == 1


@pytest.mark.parametrize(
    "config, expected",
    [
        ({"ping_interval": 0.5}, {"ping_interval": 0.5}),
        ({"max_outstanding_pings": 4}, {"max_outstanding_pings": 4}),
    ],
)
async def test_one_setting_alone_is_handed_over_and_the_other_is_left_to_nats_py(config, expected):
    kwargs = await _connect_kwargs(**config)

    for name in ("ping_interval", "max_outstanding_pings"):
        assert (name in kwargs) == (name in expected), kwargs
    assert {k: kwargs[k] for k in expected} == expected


async def test_unset_passes_neither_so_nats_pys_defaults_stay():
    kwargs = await _connect_kwargs()

    assert "ping_interval" not in kwargs and "max_outstanding_pings" not in kwargs, kwargs


def test_both_default_to_unset():
    config = ServiceConfig(name="pings")

    assert config.ping_interval is None and config.max_outstanding_pings is None


@pytest.mark.parametrize("value", [0, -1, -0.5])
def test_a_ping_interval_that_is_not_positive_is_refused(value):
    with pytest.raises(ValidationError, match=r"ping_interval\s+Input should be greater than 0"):
        ServiceConfig(name="pings", ping_interval=value)


@pytest.mark.parametrize("value", [0, -1])
def test_max_outstanding_pings_below_one_is_refused(value):
    """Zero would make the first tick, before any ping has been sent, call the connection stale."""
    with pytest.raises(
        ValidationError, match=r"max_outstanding_pings\s+Input should be greater than or equal to 1"
    ):
        ServiceConfig(name="pings", max_outstanding_pings=value)


def test_CONTROL_the_smallest_usable_values_are_accepted():
    config = ServiceConfig(name="pings", ping_interval=0.1, max_outstanding_pings=1)

    assert (config.ping_interval, config.max_outstanding_pings) == (0.1, 1)
