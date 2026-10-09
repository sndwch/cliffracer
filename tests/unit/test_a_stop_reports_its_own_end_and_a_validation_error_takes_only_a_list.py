"""`stop()` says how it ended, and `RpcValidationError` refuses a message passed as its details.

A manager built without hooks ran `stop()` to the end and left `is_stopped` False, so a
supervisor polling it waited for ever; the log said "stopped" whether teardown failed or not; and
only the first of several teardown failures could be read from what was raised. And
`RpcValidationError`, whose first parameter is `details` where every sibling's is the message,
turned `RpcValidationError("username is required")` into a "validation failed" error holding that
text as its details.
"""

from unittest.mock import AsyncMock, MagicMock

import pytest
from loguru import logger

from cliffracer import ServiceConfig
from cliffracer.client import ServiceClient
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.lifecycle import LifecycleHooks, LifecycleManager

pytestmark = pytest.mark.unit


def _hooks(**failing: Exception) -> LifecycleHooks:
    """Hooks that all succeed, except the named ones, which raise what they are given."""
    steps = (
        "stop_timers",
        "stop_health_listener",
        "cancel_subscriptions",
        "on_shutdown",
        "stop_extensions",
        "disconnect",
    )
    awaited = {name: AsyncMock(side_effect=failing.get(name)) for name in steps}
    return LifecycleHooks(
        setup_extensions=AsyncMock(),
        discover_handlers=MagicMock(),
        connect=AsyncMock(),
        ensure_streams=AsyncMock(),
        validate_dlq=MagicMock(),
        is_jetstream_active=lambda: False,
        on_startup=AsyncMock(),
        start_extensions=AsyncMock(),
        start_health_listener=AsyncMock(),
        start_timers=AsyncMock(),
        setup_subscriptions=AsyncMock(),
        **awaited,
    )


def _manager(hooks: LifecycleHooks | None) -> LifecycleManager:
    return LifecycleManager(ServiceConfig(name="svc", health_listener=False), hooks)


def _log_lines() -> tuple[list[str], int]:
    lines: list[str] = []
    return lines, logger.add(
        lambda m: lines.append(m.record["level"].name + " " + m.record["message"])
    )


async def test_a_manager_without_hooks_is_stopped_when_stop_returns():
    manager = _manager(None)

    await manager.stop()

    assert manager.is_stopped is True
    assert manager.is_running is False


async def test_a_clean_stop_logs_stopped_and_warns_of_nothing():
    manager = _manager(_hooks())
    lines, sink = _log_lines()
    try:
        await manager.stop()
    finally:
        logger.remove(sink)

    assert "INFO Service 'svc' stopped" in lines
    assert not [line for line in lines if line.startswith("WARNING")]


async def test_a_stop_whose_teardown_failed_does_not_log_stopped():
    manager = _manager(_hooks(disconnect=ConnectionError("socket would not close")))
    lines, sink = _log_lines()
    try:
        with pytest.raises(ConnectionError):
            await manager.stop()
    finally:
        logger.remove(sink)

    assert "INFO Service 'svc' stopped" not in lines
    assert any(
        line.startswith("WARNING Service 'svc' stop ended with 1 teardown") for line in lines
    )


async def test_every_teardown_failure_is_on_what_stop_raises():
    manager = _manager(
        _hooks(
            stop_timers=RuntimeError("a timer would not stop"),
            disconnect=ConnectionError("socket would not close"),
        )
    )

    with pytest.raises(RuntimeError, match="a timer would not stop") as caught:
        await manager.stop()

    assert caught.value.__notes__ == [
        "teardown also failed: ConnectionError: socket would not close"
    ]


async def test_a_single_teardown_failure_carries_no_notes():
    manager = _manager(_hooks(disconnect=ConnectionError("socket would not close")))

    with pytest.raises(ConnectionError) as caught:
        await manager.stop()

    assert not getattr(caught.value, "__notes__", [])


@pytest.mark.parametrize("message", ["username is required", 7, {"loc": ["a"]}, ["a message"]])
def test_a_message_given_as_the_details_is_refused_by_name(message):
    with pytest.raises(TypeError, match=r"details must be a list of error dicts.*message="):
        RpcValidationError(message)


def test_a_list_of_error_dicts_and_no_details_are_still_accepted():
    errors = [{"loc": ["a"], "msg": "bad"}]

    assert RpcValidationError(errors, "refused before sending").details == errors
    assert RpcValidationError().details == []
    assert RpcValidationError(None, "x").message == "x"


@pytest.mark.parametrize(
    ("wire", "expected"),
    [
        ([{"loc": ["a"], "msg": "bad"}], [{"loc": ["a"], "msg": "bad"}]),
        ("username is required", []),
        ({"loc": ["a"]}, []),
        ([{"loc": ["a"]}, "stray", 3], [{"loc": ["a"]}]),
        (None, []),
    ],
)
def test_a_validation_reply_with_malformed_details_is_still_a_validation_error(wire, expected):
    client = ServiceClient.__new__(ServiceClient)
    reply = {"success": False, "error": "validation failed", "code": "validation_failed"}
    if wire is not None:
        reply["details"] = wire

    with pytest.raises(RpcValidationError) as caught:
        client._raise_for_error(reply, "svc.rpc.create")

    assert caught.value.details == expected
