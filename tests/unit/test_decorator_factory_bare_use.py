"""Tests ensuring decorator factories raise ConfigurationError when invoked without arguments."""

import pytest
from cliffracer_auth import requires_auth, requires_permissions, requires_roles
from cliffracer_cron import cron
from cliffracer_logging import ContextualLogger, log_event_handling, log_rpc_calls
from pydantic import BaseModel

from cliffracer.core.correlation import with_correlation_id
from cliffracer.core.decorators import (
    broadcast,
    listener,
    validated_listener,
)
from cliffracer.core.decorators import timer as decorators_timer
from cliffracer.core.dependencies import dependency
from cliffracer.core.exceptions import ConfigurationError
from cliffracer.core.timer import timer as core_timer

pytestmark = pytest.mark.unit


class Model(BaseModel):
    value: int


async def handler(self, request):  # noqa: ARG001 - a stand-in for any method
    return {}


# (label, factory, the name the message must contain)
SILENT_ON_MAIN = [
    pytest.param(decorators_timer, "timer", id="decorators.timer"),
    pytest.param(core_timer, "timer", id="core.timer"),
    pytest.param(dependency, "dependency", id="dependency"),
    pytest.param(cron, "cron", id="cron"),
    pytest.param(requires_roles, "requires_roles", id="requires_roles"),
    pytest.param(requires_permissions, "requires_permissions", id="requires_permissions"),
    pytest.param(log_rpc_calls, "log_rpc_calls", id="log_rpc_calls"),
    pytest.param(log_event_handling, "log_event_handling", id="log_event_handling"),
]

# Plain decorators, not factories. Bare use is how they are meant to be
# written, so a guard on them would break correct code.
PLAIN_DECORATORS = [
    pytest.param(requires_auth, id="requires_auth"),
    pytest.param(with_correlation_id, id="with_correlation_id"),
]


@pytest.mark.parametrize("factory, name", SILENT_ON_MAIN)
def test_a_factory_used_bare_refuses_and_names_itself(factory, name):
    """The message has to name the decorator and the correct form.

    A bare `TypeError` from Python would stop the service too, but the author
    reads the traceback at import time and has to work out which of the eight
    decorators on the class caused it.
    """
    with pytest.raises(ConfigurationError) as caught:
        factory(handler)

    message = str(caught.value)
    assert f"@{name}" in message, message
    assert "decorator factory" in message, message
    # It shows the correct form, so the fix is in the error rather than in the
    # documentation the author is not currently reading.
    assert "write @" in message, message


def test_CONTROL_the_correct_form_still_registers_every_factory():
    """The other half. A guard that refused everything would pass the test
    above and take every decorator in the framework with it."""

    async def method(self):
        return {}

    assert decorators_timer(interval=60)(method)._cliffracer_timers
    assert core_timer(interval=60)(method)._cliffracer_timers
    assert cron("0 3 * * *")(method)._cliffracer_timers
    assert dependency("postgres")(method)._cliffracer_dependency["name"] == "postgres"


def test_CONTROL_a_class_first_argument_is_left_to_the_better_message():
    """`@listener(Model)` is a different mistake with its own message.

    The guard excludes classes on purpose -- a factory may take one by design,
    and `_validate_subject` explains the model-instead-of-subject case better
    than a generic factory message could. Pinning it here is what stops a later
    widening of the guard from stealing that error.
    """
    with pytest.raises(ConfigurationError) as caught:
        listener(Model)
    assert "model class" in str(caught.value), caught.value


def test_CONTROL_the_three_that_already_refused_still_do():
    """No new code covers these, which is exactly why they are asserted."""
    with pytest.raises(ConfigurationError):
        listener(handler)
    with pytest.raises(ConfigurationError):
        broadcast(handler)
    # Python's own: `schema` has no default, so a bare use cannot even build
    # the factory. Loud for a different reason, and still loud.
    with pytest.raises(TypeError):
        validated_listener(handler)


@pytest.mark.parametrize("decorator", PLAIN_DECORATORS)
def test_CONTROL_a_plain_decorator_still_takes_a_function(decorator):
    """Verify plain decorators accept functions directly."""
    wrapped = decorator(handler)
    assert callable(wrapped)
    assert wrapped.__name__ == "handler", wrapped.__name__


def test_the_auth_pair_are_the_ones_that_registered_a_wrong_handler():
    """Verify bare factory under @rpc raises ConfigurationError at decoration time."""
    from cliffracer.core.decorators import rpc

    with pytest.raises(ConfigurationError) as caught:
        rpc(requires_roles(handler))
    assert "@requires_roles" in str(caught.value), caught.value


def test_CONTROL_the_correct_form_of_the_four_new_factories_still_wraps():
    """Verify decorator factories return callables when provided valid arguments."""

    async def method(self):
        return {}

    assert callable(requires_roles("admin")(method))
    assert callable(requires_permissions("orders:write")(method))
    logger_instance = ContextualLogger("svc")
    assert callable(log_rpc_calls(logger_instance)(method))
    assert callable(log_event_handling(logger_instance)(method))
