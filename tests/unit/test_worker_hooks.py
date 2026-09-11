import json
from unittest.mock import AsyncMock

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.correlation import correlation_id_var
from cliffracer.core.extension import Extension


class Spy(Extension):
    async def setup(self, ctx):
        self.events: list[tuple] = []

    async def worker_setup(self, ctx):
        self.events.append(("setup", ctx.kind, ctx.subject, correlation_id_var.get()))

    async def worker_result(self, ctx, result, exc):
        self.events.append(("result", result, type(exc).__name__ if exc else None))

    async def worker_teardown(self, ctx):
        self.events.append(("teardown", correlation_id_var.get()))


class Svc(CliffracerService):
    spy = Spy()

    @rpc
    async def echo(self, value: str) -> str:
        return value

    @rpc
    async def boom(self) -> None:
        raise ValueError("no")

    @listener("things.happened", fanout=True)
    async def on_thing(self, a: int = 0) -> None:
        pass


def _msg(subject, data):
    m = AsyncMock()
    m.subject = subject
    m.data = json.dumps(data).encode()
    m.headers = {"correlation_id": "cid-1"}
    return m


@pytest.mark.unit
async def test_rpc_runs_setup_result_teardown_with_the_message_correlation_id():
    svc = Svc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    await svc.container._handle_rpc_request(_msg("s.rpc.echo", {"value": "x"}))
    assert svc.spy.events == [
        ("setup", "rpc", "s.rpc.echo", "cid-1"),
        ("result", "x", None),
        ("teardown", "cid-1"),
    ]
    assert correlation_id_var.get() is None


@pytest.mark.unit
async def test_a_handler_exception_still_reaches_result_and_teardown():
    svc = Svc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("s.rpc.boom", {})
    await svc.container._handle_rpc_request(msg)
    assert svc.spy.events[1] == ("result", None, "ValueError")
    assert svc.spy.events[2][0] == "teardown"
    body = json.loads(msg.respond.call_args.args[0])
    assert "Internal server error" in body["error"]
    assert "traceback" not in body

    # Opt-in with expose_internal_errors=True
    opt_svc = Svc(ServiceConfig(name="s_opt", expose_internal_errors=True))
    await opt_svc.container._setup_extensions()
    opt_svc._discover_handlers()
    opt_msg = _msg("s_opt.rpc.boom", {})
    await opt_svc.container._handle_rpc_request(opt_msg)
    opt_body = json.loads(opt_msg.respond.call_args.args[0])
    assert opt_body["error"] == "no"
    assert "traceback" in opt_body


@pytest.mark.unit
async def test_a_hook_exception_is_logged_and_does_not_change_the_result():
    class Bad(Extension):
        async def worker_setup(self, ctx):
            raise RuntimeError("hook broke")

    class S(Svc):
        bad = Bad()

    svc = S(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("s.rpc.echo", {"value": "x"})
    await svc.container._handle_rpc_request(msg)
    assert json.loads(msg.respond.call_args.args[0])["result"] == "x"


@pytest.mark.unit
async def test_events_run_the_chain_per_handler():
    svc = Svc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    await svc.container._handle_event(_msg("things.happened", {"a": 1}))
    kinds = [e[1] for e in svc.spy.events if e[0] == "setup"]
    assert kinds == ["event"]


class OrderSpy(Extension):
    """Records into a list the SERVICE owns, so two of them share one timeline."""

    def __init__(self, tag: str):
        self.tag = tag

    async def worker_setup(self, ctx):
        self.service.order.append(f"setup:{self.tag}")

    async def worker_result(self, ctx, result, exc):
        self.service.order.append(f"result:{self.tag}")

    async def worker_teardown(self, ctx):
        self.service.order.append(f"teardown:{self.tag}")


class OrderSvc(CliffracerService):
    first = OrderSpy("first")
    second = OrderSpy("second")

    def __init__(self, config):
        self.order: list[str] = []
        super().__init__(config)

    @rpc
    async def echo(self, value: str) -> str:
        return value


@pytest.mark.unit
async def test_result_and_teardown_hooks_run_in_reverse_declaration_order():
    """Ensure setup hooks run in declaration order, and result and teardown hooks run in reverse order."""
    svc = OrderSvc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    await svc.container._handle_rpc_request(_msg("s.rpc.echo", {"value": "x"}))
    assert svc.order == [
        "setup:first",
        "setup:second",
        "result:second",
        "result:first",
        "teardown:second",
        "teardown:first",
    ]


# --------------------------------------------------------------------------
# Dispatch kinds with behavioral hook chain tests.
# --------------------------------------------------------------------------


@pytest.mark.unit
async def test_async_rpc_runs_the_chain():
    svc = Svc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    await svc.container._handle_async_request(_msg("s.async.echo", {"value": "y"}))

    assert svc.spy.events == [
        ("setup", "async_rpc", "s.async.echo", "cid-1"),
        ("result", "y", None),
        ("teardown", "cid-1"),
    ]
    assert correlation_id_var.get() is None


@pytest.mark.unit
async def test_jetstream_events_run_the_chain_and_ack():
    """Verify JetStream event execution triggers extension hook chain and message ack."""
    svc = Svc(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _msg("things.happened", {"a": 1})
    await svc.container._handle_jetstream_event(msg)

    kinds = [e[1] for e in svc.spy.events if e[0] == "setup"]
    assert kinds == ["event"], svc.spy.events
    assert [e[0] for e in svc.spy.events] == ["setup", "result", "teardown"]
    msg.ack.assert_awaited_once()
    assert correlation_id_var.get() is None


@pytest.mark.unit
async def test_a_failing_jetstream_handler_still_reaches_result_and_teardown():
    """A nak path must not skip the hooks: an extension that only ran on success
    would report timings and metrics for the happy path alone."""

    class Boom(CliffracerService):
        spy = Spy()

        @listener("things.happened", fanout=True)
        async def on_thing(self, a: int = 0) -> None:
            raise ValueError("no")

    svc = Boom(ServiceConfig(name="s"))
    await svc.container._setup_extensions()
    svc._discover_handlers()

    msg = _msg("things.happened", {"a": 1})
    msg.metadata.num_delivered = 1
    await svc.container._handle_jetstream_event(msg)

    assert [e[0] for e in svc.spy.events] == ["setup", "result", "teardown"]
    assert svc.spy.events[1] == ("result", None, "ValueError")
    msg.ack.assert_not_awaited()


@pytest.mark.unit
async def test_describe_runs_the_chain():
    """The fifth callback. `{service}.describe` publishes the whole API
    surface, so it is the last entry point that should be invisible to
    extensions -- an AuthExtension that refuses unauthenticated callers has to
    be able to refuse this one too."""
    svc = Svc(ServiceConfig(name="s", version="7.7.7"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("s.describe", {})

    await svc.container._handle_describe_request(msg)

    kinds = [e for e in svc.spy.events if e[0] == "setup"]
    assert kinds == [("setup", "describe", "s.describe", "cid-1")]
    assert svc.spy.events[-1] == ("teardown", "cid-1")
    assert correlation_id_var.get() is None

    body = json.loads(msg.respond.await_args.args[0].decode())
    assert body["service"] == "s" and body["version"] == "7.7.7"
    assert [m["name"] for m in body["methods"]] == ["boom", "echo"]


@pytest.mark.unit
async def test_a_refused_describe_answers_rather_than_hanging_the_caller():
    """RejectMessage from worker_setup is how an extension refuses. The caller
    is waiting on a reply, so a refusal that answered nothing would read as a
    service that is not running."""
    from cliffracer.core.extension import Extension, RejectMessage

    class Deny(Extension):
        async def worker_setup(self, ctx):
            if ctx.kind == "describe":
                raise RejectMessage("no token")

    class Guarded(CliffracerService):
        deny = Deny()

        @rpc
        async def echo(self, value: str) -> str:
            return value

    svc = Guarded(ServiceConfig(name="g"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _msg("g.describe", {})

    await svc.container._handle_describe_request(msg)

    body = json.loads(msg.respond.await_args.args[0].decode())
    assert body["error"] == "refused: no token"
    assert "methods" not in body
