"""The dispatch layer logs through the service's logger as it is when it logs, not as it was built."""

from unittest.mock import AsyncMock

import pytest
from loguru import logger

from cliffracer import CliffracerService, ServiceConfig, listener

pytestmark = pytest.mark.unit


class Recorder:
    """A logger that keeps what it is given: `(level, message)`."""

    def __init__(self) -> None:
        self.lines: list[tuple[str, str]] = []

    def _record(self, level: str):
        def log(message: str, *args, **kwargs) -> None:
            self.lines.append((level, message.format(*args) if args else message))

        return log

    def __getattr__(self, level: str):
        if level in {"debug", "info", "warning", "error", "exception"}:
            return self._record(level)
        raise AttributeError(level)

    def bind(self, **_extra) -> "Recorder":
        return self


class Mixin:
    """What `CorrelationLoggerMixin` does: build the service, then replace its logger."""

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self.logger = Recorder()


class Svc(Mixin, CliffracerService):
    @listener("orders.created", fanout=True)
    async def on_order(self, subject: str) -> None:
        pass


def _collaborators(svc):
    dispatcher = svc.container.dispatcher
    return {
        "pipeline": dispatcher.pipeline,
        "dlq": dispatcher.dlq,
        "rpc": dispatcher.rpc,
        "events": dispatcher.events,
        "jetstream": dispatcher.jetstream,
    }


@pytest.mark.parametrize("name", ["pipeline", "dlq", "rpc", "events", "jetstream"])
def test_every_collaborator_writes_to_the_logger_the_service_has_now(name):
    svc = Svc(ServiceConfig(name="replaced"))

    _collaborators(svc)[name].logger.warning("from the collaborator")

    assert svc.logger.lines == [("warning", "from the collaborator")]


def test_the_dispatcher_itself_follows_the_service_too():
    svc = Svc(ServiceConfig(name="replaced"))

    assert svc.container.dispatcher.logger is svc.logger


@pytest.mark.asyncio
async def test_a_dispatch_path_line_lands_in_the_replaced_logger():
    """An undecodable event, through the real dispatcher: the decode error is reported by the
    event dispatcher, and it is the replaced logger that hears it."""
    svc = Svc(ServiceConfig(name="replaced"))
    svc._discover_handlers()
    svc.container.nc = AsyncMock()
    message = AsyncMock()
    message.subject = "orders.created"
    message.data = b"{not json"
    message.headers = {"Content-Type": "application/json"}
    message.reply = None

    sunk: list[str] = []
    sink = logger.add(sunk.append, level="DEBUG")
    try:
        await svc.container._handle_event(message)
    finally:
        logger.remove(sink)

    assert any(
        level == "error" and message_text.startswith("Error decoding payload for event on")
        for level, message_text in svc.logger.lines
    ), svc.logger.lines
    assert not any("Error decoding payload" in line for line in sunk), "it went to the old logger"


def test_a_logger_assigned_to_the_dispatcher_is_followed_by_its_collaborators():
    svc = CliffracerService(ServiceConfig(name="plain"))
    replaced = Recorder()

    svc.container.dispatcher.logger = replaced
    svc.container.dispatcher.rpc.logger.error("after the assignment")

    assert replaced.lines == [("error", "after the assignment")]


def test_CONTROL_without_a_replacement_the_lines_go_where_they_went():
    svc = CliffracerService(ServiceConfig(name="plain"))
    sunk: list[str] = []
    sink = logger.add(lambda m: sunk.append(m.record["message"]), level="DEBUG")
    try:
        svc.container.dispatcher.events.logger.warning("still the default logger")
    finally:
        logger.remove(sink)

    assert sunk == ["still the default logger"]
