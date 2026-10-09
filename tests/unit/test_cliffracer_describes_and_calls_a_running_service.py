"""`cliffracer describe` and `cliffracer call` against a service running in process.

Each service runs its own start over an `InMemoryBroker` (`ServiceTestHarness(broker=)`), and the
command dials that broker in place of a server, so what is pinned is the whole exchange: the
describe it sends, the pre-flight it does from the reply, the request it sends, and what it writes
and exits with. The replies a real service is hard to make send (busy, a deadline, a streaming
method, a reply that is not one) come from a responder answering on the service's subjects.
"""

import asyncio
import io
import json
from collections.abc import AsyncIterator

import pytest
from pydantic import BaseModel, Field

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.cli.live_service import DEFAULT_URL
from cliffracer.cli.main import build_parser
from cliffracer.cli.operate import (
    EXIT_ARGUMENTS,
    EXIT_BUSY,
    EXIT_DEADLINE,
    EXIT_NO_BROKER,
    EXIT_NO_SERVICE,
    EXIT_REFUSED,
    EXIT_SERVER_ERROR,
    EXIT_UNDESCRIBABLE,
    EXIT_UNKNOWN_METHOD,
    EXIT_USAGE,
    run_async,
)
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext
from cliffracer.core.typed_rpc import return_type_ref
from cliffracer.testing import InMemoryBroker, ServiceTestHarness

pytestmark = pytest.mark.unit


class Item(BaseModel):
    sku: str
    quantity: int = Field(gt=0)


class Orders(CliffracerService):
    """Takes orders."""

    @rpc
    async def place(self, item: Item, priority: int = 0) -> dict[str, int]:
        """Place one order."""
        return {"quantity": item.quantity, "priority": priority}

    @rpc
    async def ping(self, word: str) -> str:
        return word


class Gate(Extension):
    async def worker_setup(self, ctx: WorkerContext) -> None:
        if ctx.kind == "rpc" and ctx.headers.get("authorization") != "bearer ok":
            raise RejectMessage("no token")


class GatedOrders(Orders):
    gate = Gate()


@pytest.fixture(autouse=True)
def _no_prefix(monkeypatch):
    monkeypatch.delenv("CLIFFRACER_SUBJECT_PREFIX", raising=False)


@pytest.fixture
async def broker():
    return InMemoryBroker()


async def cli(broker, *argv, dialer=None):
    out, err = io.StringIO(), io.StringIO()
    args = build_parser().parse_args(list(argv))
    code = await run_async(args, dialer=dialer or broker.connect, out=out, err=err)
    return code, out.getvalue(), err.getvalue()


def sent_to(broker, subject):
    return [p for p in broker.published if p.subject == subject]


def _config(name="orders"):
    return ServiceConfig(name=name, health_port=0)


# --- describe --------------------------------------------------------------------------------


async def test_describe_prints_the_methods_and_their_types(broker):
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        code, out, err = await cli(broker, "describe", "orders")

    assert (code, err) == (0, ""), err
    assert out.startswith("orders ")
    assert "  place(item: Item, priority: int = 0) -> dict[str, int]" in out
    assert "      Place one order." in out
    assert "  ping(word: str) -> str" in out


async def test_describe_json_is_the_description_the_service_sent(broker):
    async with ServiceTestHarness(Orders, config=_config(), broker=broker) as harness:
        code, out, _ = await cli(broker, "describe", "orders", "--json")
        sent = await harness.describe()

    assert code == 0
    assert json.loads(out) == sent


async def test_describe_with_nothing_listening_says_so(broker):
    code, out, err = await cli(broker, "describe", "orders")

    assert (code, out) == (EXIT_NO_SERVICE, "")
    assert "nothing answered describe for 'orders'" in err


async def test_a_broker_that_cannot_be_reached_is_named(broker):
    async def unreachable(url, **options):
        raise ConnectionRefusedError("connection refused")

    code, _, err = await cli(broker, "describe", "orders", dialer=unreachable)

    assert code == EXIT_NO_BROKER
    assert f"no broker reachable at {DEFAULT_URL}: connection refused" in err


async def test_a_broker_that_refuses_the_credentials_is_told_apart(broker):
    from nats.errors import AuthorizationError, NoServersError

    async def refusing(url, *, error_cb, **options):
        await error_cb(AuthorizationError())
        raise NoServersError

    code, _, err = await cli(
        broker, "describe", "orders", "--server", "nats://u:secret@h:4222", dialer=refusing
    )

    assert code == EXIT_NO_BROKER
    assert "refused this client's credentials" in err
    assert "secret" not in err


# --- call: what reaches the service ------------------------------------------------------------


async def test_a_call_prints_the_result_as_json_on_stdout(broker):
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        code, out, err = await cli(
            broker,
            "call",
            "orders.place",
            "--json-args",
            '{"item": {"sku": "bolt", "quantity": 3}}',
            "--arg",
            "priority=2",
        )

    assert (code, err) == (0, ""), err
    assert json.loads(out) == {"quantity": 3, "priority": 2}


async def test_the_request_goes_to_the_rpc_subject_with_its_headers(broker):
    async with ServiceTestHarness(GatedOrders, config=_config(), broker=broker):
        code, out, err = await cli(
            broker,
            "call",
            "orders.ping",
            "--arg",
            "word=hi",
            "--header",
            "authorization=bearer ok",
        )

    assert (code, json.loads(out)) == (0, "hi"), err
    (request,) = sent_to(broker, "orders.rpc.ping")
    assert request.headers["authorization"] == "bearer ok"
    assert request.headers["Content-Type"] == "application/json"
    assert request.headers["X-Correlation-ID"]


async def test_dry_run_shows_the_request_and_sends_nothing(broker):
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        code, out, err = await cli(broker, "call", "orders.ping", "--arg", "word=hi", "--dry-run")

    assert (code, err) == (0, ""), err
    shown = json.loads(out)
    assert (shown["subject"], shown["payload"]) == ("orders.rpc.ping", {"word": "hi"})
    assert sent_to(broker, "orders.rpc.ping") == []


# --- call: the pre-flight, nothing sent ----------------------------------------------------------


@pytest.mark.parametrize(
    ("argv", "code", "said"),
    [
        (
            ["orders.nope"],
            EXIT_UNKNOWN_METHOD,
            "orders has no method 'nope'; it has ['ping', 'place']",
        ),
        (["orders.>"], EXIT_UNKNOWN_METHOD, "orders has no method '>'"),
        (["orders.ping"], EXIT_ARGUMENTS, "ping needs ['word']"),
        (["orders.ping", "--arg", "word=hi", "--arg", "loud=1"], EXIT_ARGUMENTS, "takes no 'loud'"),
        (
            ["orders.ping", "--json-args", '{"word": "hi", "loud": 1}'],
            EXIT_ARGUMENTS,
            "ping takes no ['loud']; it takes ['word']",
        ),
        (["orders.ping", "--json-args", '{"word": 3}'], EXIT_ARGUMENTS, "word is a str, got int 3"),
        (
            ["orders.place", "--arg", "item=x"],
            EXIT_ARGUMENTS,
            "is given with --json-args, not --arg",
        ),
        (["orders.place", "--json-args", "[1]"], EXIT_USAGE, "must be a JSON object"),
    ],
)
async def test_a_call_the_description_rules_out_is_refused_before_sending(broker, argv, code, said):
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        got, out, err = await cli(broker, "call", *argv)

    assert (got, out) == (code, ""), err
    assert said in err, err
    assert [p.subject for p in broker.published if ".rpc." in p.subject] == []


async def test_a_name_with_more_dots_reaches_only_a_describe_subject(broker):
    """`a.b.>` is the method `>` of a service called `a.b`: it is asked for its description, which
    nobody gives, so nothing is sent on any subject."""
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        code, _, err = await cli(broker, "call", "orders.place.>")

    assert code == EXIT_NO_SERVICE, err
    assert "nothing answered orders.place.>" in err
    # The describe found no responder, so nothing at all was published.
    assert broker.published == ()


async def test_a_bad_namespace_is_a_usage_error(broker):
    code, _, err = await cli(broker, "call", "orders.ping", "--namespace", "a b")

    assert code == EXIT_USAGE
    assert "cliffracer call: --namespace:" in err
    assert broker.published == ()


# --- call: what the service answered ------------------------------------------------------------


async def test_a_value_the_service_refuses_is_its_validation_error_on_stderr(broker):
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        code, out, err = await cli(
            broker, "call", "orders.place", "--json-args", '{"item": {"sku": "b", "quantity": 0}}'
        )

    assert (code, out) == (EXIT_ARGUMENTS, "")
    error = json.loads(err.splitlines()[0])
    assert error["code"] == "validation_failed"
    assert error["details"], error


async def test_a_refusal_is_its_own_exit_code(broker):
    async with ServiceTestHarness(GatedOrders, config=_config(), broker=broker):
        code, out, err = await cli(broker, "call", "orders.ping", "--arg", "word=hi")

    assert (code, out) == (EXIT_REFUSED, "")
    assert json.loads(err.splitlines()[0])["code"] == "refused"


async def _answering(broker, description, answer):
    """A responder that describes itself as `description` and answers every call with `answer`."""
    conn = await broker.connect()

    async def describe(msg):
        await msg.respond(json.dumps(description).encode())

    async def call(msg):
        await msg.respond(answer if isinstance(answer, bytes) else json.dumps(answer).encode())

    await conn.subscribe("svc.describe", cb=describe)
    await conn.subscribe("svc.rpc.*", cb=call)
    return conn


def _description(returns=None):
    return {
        "service": "svc",
        "version": "1",
        "methods": [
            {
                "name": "go",
                "params": [],
                "returns": returns or {"kind": "scalar", "name": "str"},
            }
        ],
    }


@pytest.mark.parametrize(
    ("answer", "code", "said"),
    [
        ({"success": False, "code": "busy", "error": "busy", "retry_after": 2}, EXIT_BUSY, "busy"),
        (
            {"success": False, "code": "deadline_exceeded", "error": "late"},
            EXIT_DEADLINE,
            "deadline",
        ),
        (
            {"success": False, "code": "unknown_method", "error": "gone"},
            EXIT_UNKNOWN_METHOD,
            "gone",
        ),
        ({"success": False, "code": "internal", "error": "boom"}, EXIT_SERVER_ERROR, "boom"),
        (b"not json", EXIT_SERVER_ERROR, "answered with no JSON"),
        ([1, 2], EXIT_SERVER_ERROR, "answered with no envelope"),
    ],
)
async def test_each_class_of_answer_has_its_exit_code(broker, answer, code, said):
    conn = await _answering(broker, _description(), answer)
    try:
        got, out, err = await cli(broker, "call", "svc.go")
    finally:
        await conn.close()

    assert (got, out) == (code, ""), err
    assert said.lower() in err.lower(), err


async def test_the_retry_hint_of_a_busy_service_reaches_the_error_object(broker):
    conn = await _answering(
        broker, _description(), {"success": False, "code": "busy", "error": "b", "retry_after": 2}
    )
    try:
        _, _, err = await cli(broker, "call", "svc.go")
    finally:
        await conn.close()

    assert json.loads(err.splitlines()[0])["retry_after"] == 2


async def test_a_streaming_method_is_refused_by_name(broker):
    conn = await _answering(broker, _description({"kind": "stream", "item": {}}), {"success": True})
    try:
        code, out, err = await cli(broker, "call", "svc.go")
    finally:
        await conn.close()

    assert (code, out) == (EXIT_UNDESCRIBABLE, "")
    assert "svc.go streams its reply" in err
    assert sent_to(broker, "svc.rpc.go") == []


@pytest.mark.parametrize(
    ("returns", "shown"),
    [
        pytest.param(AsyncIterator[int], "stream[int]", id="scalar"),
        pytest.param(AsyncIterator[list[Item]], "stream[list[Item]]", id="nested"),
    ],
)
async def test_describe_prints_a_streaming_method_with_its_item(broker, returns, shown):
    """The TypeRef is the one the service would serve, read off the annotation: a service does not
    start with a streaming handler yet, so no service here can describe one itself."""
    conn = await _answering(broker, _description(return_type_ref(returns)), {"success": True})
    try:
        code, out, err = await cli(broker, "describe", "svc")
    finally:
        await conn.close()

    assert (code, err) == (0, ""), err
    assert f"  go() -> {shown}" in out.splitlines()


async def test_a_reply_that_is_not_a_description_is_named(broker):
    conn = await _answering(broker, {"service": "svc"}, {"success": True})
    try:
        code, _, err = await cli(broker, "describe", "svc")
    finally:
        await conn.close()

    assert code == EXIT_UNDESCRIBABLE
    assert "not a description: version is missing" in err


# --- call: the budget the request carries --------------------------------------------------------


#: How long the slow handler runs unless cut off; a run that misses the cut waits this out.
CRAWL = 8.0


class Slow(CliffracerService):
    """A handler that runs far past any budget unless something cuts it off."""

    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.cancelled = asyncio.Event()

    @rpc
    async def crawl(self) -> int:
        try:
            await asyncio.sleep(CRAWL)
        except asyncio.CancelledError:
            self.cancelled.set()
            raise
        return 0


async def test_the_request_carries_the_commands_timeout_as_its_budget(broker):
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        code, _, err = await cli(
            broker, "call", "orders.ping", "--arg", "word=hi", "--timeout", "2.5"
        )

    assert code == 0, err
    (request,) = sent_to(broker, "orders.rpc.ping")
    assert request.headers["Cliffracer-Timeout-Ms"] == "2500"


async def test_dry_run_shows_the_budget_it_would_send(broker):
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        code, out, err = await cli(
            broker, "call", "orders.ping", "--arg", "word=hi", "--timeout", "2.5", "--dry-run"
        )

    assert (code, err) == (0, ""), err
    assert json.loads(out)["headers"]["Cliffracer-Timeout-Ms"] == "2500"


@pytest.mark.parametrize("name", ["Cliffracer-Timeout-Ms", "cliffracer-timeout-ms"])
async def test_a_budget_given_as_a_header_is_sent_as_given_and_once(broker, name):
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        code, _, err = await cli(
            broker, "call", "orders.ping", "--arg", "word=hi", "--header", f"{name}=777"
        )

    assert code == 0, err
    (request,) = sent_to(broker, "orders.rpc.ping")
    budgets = {k: v for k, v in request.headers.items() if k.lower() == "cliffracer-timeout-ms"}
    assert budgets == {name: "777"}


async def test_a_handler_called_through_the_cli_is_cut_at_the_commands_timeout(broker):
    """The service bounds the handler by the budget the command sends; without it the handler
    would crawl on for `CRAWL` seconds after the command gave up."""
    service = Slow(_config("slow"))
    async with ServiceTestHarness(service, broker=broker):
        began = asyncio.get_running_loop().time()
        await cli(broker, "call", "slow.crawl", "--timeout", "0.3")
        try:
            await asyncio.wait_for(service.cancelled.wait(), 5.0)
        except TimeoutError:
            sent = sent_to(broker, "slow.rpc.crawl")[0].headers
            pytest.fail(
                f"the handler was still running 5s after a call with --timeout 0.3: nothing cut "
                f"it off. The request carried {sent!r}"
            )
        took = asyncio.get_running_loop().time() - began

    assert took < 2.0, f"the handler was cut {took:.2f}s after the call, not at its 0.3s budget"


@pytest.mark.parametrize(
    "timeout",
    [
        pytest.param("inf", id="no-deadline"),
        pytest.param("86401", id="over-the-one-day-ceiling"),
    ],
)
async def test_a_wait_the_service_would_not_read_as_a_budget_sends_none(broker, timeout):
    """`inf` means no deadline, and over a day is past what a service reads: the call is sent
    with no budget, and the service applies its own cap."""
    async with ServiceTestHarness(Orders, config=_config(), broker=broker):
        code, out, err = await cli(
            broker, "call", "orders.ping", "--arg", "word=hi", "--timeout", timeout
        )

    assert (code, json.loads(out)) == (0, "hi"), err
    (request,) = sent_to(broker, "orders.rpc.ping")
    assert not any(k.lower() == "cliffracer-timeout-ms" for k in request.headers), request.headers
