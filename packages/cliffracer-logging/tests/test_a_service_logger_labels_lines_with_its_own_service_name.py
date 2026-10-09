"""A `ContextualLogger` writes the service name it was given, not the last one configured.

Loguru's global `extra` is process-wide, so in a process with more than one service the
`service` on a record written through the plain `logger` is whichever service configured
logging last. The logger a service gets from `get_service_logger` says which service it is.
"""

from types import SimpleNamespace

import pytest
from cliffracer_logging import log_rpc_calls
from cliffracer_logging.config import ContextualLogger, LoggingConfig, get_service_logger
from loguru import logger

pytestmark = pytest.mark.unit


@pytest.fixture
def extras():
    """The ``extra`` of every record a plain sink receives, with logging configured for 'gateway'."""
    logger.remove()
    logger.configure(extra={})
    LoggingConfig.configure(service_name="gateway", enable_console=False, enable_file=False)
    seen: list[dict] = []
    logger.add(lambda message: seen.append(dict(message.record["extra"])), level="DEBUG")
    yield seen
    logger.remove()
    logger.configure(extra={})


def test_a_logger_for_another_service_labels_its_own_lines(extras):
    get_service_logger("orders", component="api").info("placed")

    assert extras[-1]["service"] == "orders"
    assert extras[-1]["component"] == "api"


def test_a_derived_logger_keeps_the_service_name(extras):
    get_service_logger("orders").with_context(stage="parse").with_context(step=2).info("a line")

    assert extras[-1]["service"] == "orders"
    assert extras[-1]["stage"] == "parse"
    assert extras[-1]["step"] == 2


def test_two_loggers_in_one_process_each_name_their_own_service(extras):
    orders = get_service_logger("orders")
    billing = ContextualLogger("billing")

    orders.info("one")
    billing.info("two")
    orders.info("three")

    assert [e["service"] for e in extras] == ["orders", "billing", "orders"]


def test_a_service_passed_on_one_call_replaces_the_name_for_that_line_only(extras):
    log = get_service_logger("orders")

    log.info("elsewhere", service="inventory")
    log.info("home")

    assert [e["service"] for e in extras] == ["inventory", "orders"]


async def test_a_decorated_handler_still_names_the_service_it_ran_on(extras):
    log = get_service_logger("orders")

    class Handlers:
        config = SimpleNamespace(name="inventory")

        @log_rpc_calls(log)
        async def reserve(self, sku):
            return {"sku": sku}

    await Handlers().reserve(sku="a")

    assert [e["service"] for e in extras] == ["inventory", "inventory"]


def test_CONTROL_a_plain_logger_line_carries_the_service_configured_last(extras):
    """The global is still set; only a contextual logger names its own service."""
    logger.info("a plain line")

    assert extras[-1]["service"] == "gateway"
