"""The logging package redacts, formats, streams, labels and stops as its docstrings say."""

import asyncio
import inspect
import json

import pytest
from cliffracer_logging import (
    LoggingConfig,
    LoggingExtension,
    get_service_logger,
    log_event_handling,
    log_rpc_calls,
    redact_sensitive_log_fields,
    setup_correlation_logging,
)
from cliffracer_logging.config import DEFAULT_NATS_SINK_MAX_PENDING, NatsSinkStats
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig

pytestmark = pytest.mark.unit


def _handlers():
    return set(logger._core.handlers)


@pytest.fixture
def records():
    seen = []
    sink = logger.add(lambda m: seen.append(m.record), level="DEBUG")
    yield seen
    logger.remove(sink)


@pytest.fixture
def added_sinks():
    """Remove the sinks a test added, for calls that do not return their ids."""
    before = _handlers()
    yield
    for handler in _handlers() - before:
        logger.remove(handler)


class _Nc:
    async def publish(self, subject, payload):
        pass


# --- LoggingConfig and the NATS sink ----------------------------------------


def test_a_credential_inside_a_list_is_redacted_and_the_list_stays_a_list():
    out = redact_sensitive_log_fields({"items": [{"password": "p", "keep": 1}, "plain"]})
    assert out == {"items": [{"password": "[REDACTED]", "keep": 1}, "plain"]}


def test_the_nats_sinks_backlog_defaults_to_the_1000_the_readme_gives():
    assert DEFAULT_NATS_SINK_MAX_PENDING == 1000
    default = inspect.signature(LoggingConfig.add_nats_sink).parameters["max_pending"].default
    assert default == 1000


def test_sink_stats_compare_and_print_by_their_counters_alone():
    first, second = NatsSinkStats(), NatsSinkStats()
    second._last_said = "BacklogFull"
    assert first == second
    assert "_lock" not in repr(first) and "_last_said" not in repr(first)


def test_a_structured_file_line_carries_the_message_alone_as_its_text(tmp_path):
    ids = LoggingConfig.configure(
        "svc", log_dir=str(tmp_path), enable_console=False, replace_existing=False
    )
    try:
        logger.info("hello")
        logger.complete()
    finally:
        for handler in ids:
            logger.remove(handler)
    texts = [json.loads(line)["text"] for line in (tmp_path / "svc.log").read_text().splitlines()]
    assert "hello\n" in texts


async def test_a_nats_sink_may_allow_a_backlog_of_one():
    handler = LoggingConfig.add_nats_sink(
        "svc", _Nc(), config=ServiceConfig(name="svc"), max_pending=1
    )
    logger.remove(handler)


def test_a_record_that_arrives_after_the_sinks_loop_closed_is_counted_as_such():
    stats = NatsSinkStats()
    loop = asyncio.new_event_loop()

    async def attach():
        handler = LoggingConfig.add_nats_sink(
            "closedsvc", _Nc(), config=ServiceConfig(name="closedsvc"), stats=stats
        )
        # The sink's own "streaming enabled" line is bound to this service: let it publish first.
        await logger.complete()
        for _ in range(3):
            await asyncio.sleep(0)
        assert stats.snapshot()["published"] == 1
        return handler

    handler = loop.run_until_complete(attach())
    loop.close()
    try:
        logger.bind(service="closedsvc").info("after close")
        logger.complete()
    finally:
        logger.remove(handler)
    snapshot = stats.snapshot()
    assert (snapshot["dropped"], snapshot["last_error"]) == (1, "EventLoopClosed")


# --- the handler decorators -------------------------------------------------


def test_a_decorated_function_with_no_code_object_is_logged_at_its_own_name(records):
    wrapped = log_rpc_calls(get_service_logger("svc"))(len)
    assert wrapped([1, 2]) == 2
    assert any(record["function"] == "len" for record in records)


class _DictConfigured:
    config = {"name": "orders"}


def test_a_service_whose_config_is_a_dict_is_named_by_its_name_key(records):
    @log_rpc_calls(get_service_logger("x"))
    def handle(self):
        return 1

    handle(_DictConfigured())
    services = {r["extra"].get("service") for r in records if "RPC call" in r["message"]}
    assert services == {"orders"}


class _Svc:
    config = ServiceConfig(name="svc")


def _positional_counts(records):
    return [
        r["extra"]["positional_arg_count"] for r in records if "positional_arg_count" in r["extra"]
    ]


def test_the_rpc_decorator_counts_positional_arguments_past_the_service(records):
    """A first argument of None is not a service instance, so it is counted."""

    @log_rpc_calls(get_service_logger("x"))
    def handle(*args):
        return None

    handle(_Svc())
    handle(None, 5)
    assert _positional_counts(records) == [0, 0, 2, 2]


def test_the_event_decorator_counts_positional_arguments_past_the_service(records):
    @log_event_handling(get_service_logger("x"))
    def on_event(*args):
        return None

    on_event(_Svc())
    on_event(None, 5)
    assert _positional_counts(records) == [0, 0, 2, 2]


# --- setup_correlation_logging ----------------------------------------------


def test_a_custom_log_format_is_the_one_the_console_uses(capsys, added_sinks):
    setup_correlation_logging(
        "svc", log_format="CUSTOM {message}", enable_file=False, replace_existing=False
    )
    logger.info("hello")
    logger.complete()
    assert "CUSTOM hello" in capsys.readouterr().out


def test_the_correlation_log_file_is_text_in_its_columns(tmp_path, added_sinks):
    setup_correlation_logging("svc", log_dir=str(tmp_path), replace_existing=False)
    logger.info("hello")
    logger.complete()
    mine = [
        line
        for line in (tmp_path / "svc.log").read_text().splitlines()
        if line.endswith(" | hello")
    ]
    assert len(mine) == 1
    with pytest.raises(json.JSONDecodeError):
        json.loads(mine[0])


# --- LoggingExtension -------------------------------------------------------


def _service(**extension):
    class Svc(CliffracerService):
        logging = LoggingExtension(**extension)

    return Svc(ServiceConfig(name="logsvc", health_port=0))


async def test_an_extension_with_to_nats_off_attaches_no_sink_even_when_connected():
    svc = _service(to_nats=False)
    await svc.container._setup_extensions()
    svc.nc = _Nc()
    before = _handlers()
    await svc.logging.start()
    try:
        assert _handlers() == before
        assert svc.logging.health_details()["streaming"] is False
    finally:
        await svc.logging.stop()


async def test_stopping_an_extension_that_never_streamed_leaves_every_other_sink():
    mine = logger.add(lambda m: None, level="DEBUG")
    try:
        svc = _service(to_nats=False)
        await svc.container._setup_extensions()
        await svc.logging.start()
        await svc.logging.stop()
        assert mine in _handlers()
    finally:
        if mine in _handlers():
            logger.remove(mine)
