"""Tests verifying ServiceClient encoding, unwrapping, and error mapping."""

import json

import pytest
from pydantic import BaseModel

from cliffracer import rpc
from cliffracer.client import (
    ClientError,
    ClientOutOfDate,
    RpcNoResponders,
    RpcRefused,
    RpcServerError,
    RpcTimeout,
    RpcUnknownMethod,
    RpcValidationError,
    ServiceClient,
)
from cliffracer.testing import refuse_a_reply_with_no_subject

pytestmark = pytest.mark.unit


class Order(BaseModel):
    sku: str
    qty: int = 1


class FakeNC:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.timeouts = []
        self.is_closed = False
        self._cbs = []

    async def request(self, subject, payload, timeout=None, headers=None):
        self.sent.append((subject, json.loads(payload or b"{}"), headers))
        self.timeouts.append(timeout)
        item = self.replies.pop(0)
        if isinstance(item, Exception):
            raise item

        class R:
            data = json.dumps(item).encode()

        return R()


class OrdersClient(ServiceClient):
    SERVICE = "orders"
    VERSION = "1"
    DESCRIPTION_HASH = "sha256:x"
    SIGNATURES = {"create": "sha256:sig-create"}

    async def create(self, order: Order, note: str = "") -> Order:
        return await self._call(
            "create",
            {"order": self._encode(order, Order), "note": self._encode(note, str)},
            Order,
        )


def _desc(sig="sha256:sig-create", methods=("create",)):
    return {
        "service": "orders",
        "version": "1",
        "description_hash": "sha256:x",
        "methods": [
            {
                "name": m,
                "doc": None,
                "params": [],
                "returns": {"kind": "scalar", "name": "none"},
                "signature_hash": sig,
            }
            for m in methods
        ],
    }


async def test_a_call_encodes_models_sends_the_subject_and_validates_the_result():
    nc = FakeNC(
        [
            _desc(),
            {
                "success": True,
                "result": {"sku": "a", "qty": 2},
                "timestamp": "t",
                "correlation_id": "c",
            },
        ]
    )
    c = OrdersClient(nc, service="orders", namespace="t1", headers={"authorization": "bearer tok"})
    out = await c.create(Order(sku="a", qty=2))
    assert out == Order(sku="a", qty=2)
    subject, body, headers = nc.sent[1]
    assert subject == "t1.orders.rpc.create"
    assert body == {"order": {"sku": "a", "qty": 2}, "note": ""}
    assert headers["authorization"] == "bearer tok"
    assert headers["correlation_id"], "a request with no id at all"
    assert headers["X-Correlation-ID"] == headers["correlation_id"]


async def test_a_method_the_service_lacks_is_reported_as_missing_not_changed():
    """The missing branch of the drift check, and the call is never sent.

    A client that is out of date must not put the request on the wire: the
    describe is the only subject asked.
    """
    nc = FakeNC([_desc(sig="sha256:OTHER", methods=())])
    c = OrdersClient(nc, service="orders")
    with pytest.raises(ClientOutOfDate) as e:
        await c.create(Order(sku="a"))
    assert e.value.missing == ["create"]
    assert e.value.changed == []
    assert [subject for subject, _, _ in nc.sent] == ["orders.describe"]


async def test_a_changed_signature_is_reported_as_changed_not_missing():
    """The other branch of the drift check, and the unit suite missed it.

    Mutating `verify` to stop comparing hashes (`elif False:`) left every unit
    test green and reddened only the round-trip test, because the case above
    exercises `missing` and nothing exercised `changed`. Both branches are the
    error a regenerated client is supposed to prevent, so both are pinned.
    """
    nc = FakeNC([_desc(sig="sha256:OTHER")])
    c = OrdersClient(nc, service="orders")
    with pytest.raises(ClientOutOfDate) as e:
        await c.create(Order(sku="a"))
    assert e.value.changed == ["create"]
    assert e.value.missing == []
    assert [subject for subject, _, _ in nc.sent] == ["orders.describe"]


async def test_verify_ignores_methods_the_service_added():
    nc = FakeNC(
        [_desc(methods=("create", "extra")), {"success": True, "result": {"sku": "a", "qty": 1}}]
    )
    c = OrdersClient(nc, service="orders")
    await c.create(Order(sku="a"))
    assert len(nc.sent) == 2  # describe once, then the call


@pytest.mark.parametrize(
    ("reply", "exc"),
    [
        (
            {
                "success": False,
                "error": "validation failed",
                "details": [{"loc": ["order", "sku"]}],
            },
            RpcValidationError,
        ),
        ({"error": "Unknown method: create"}, RpcUnknownMethod),
        ({"error": "refused: unauthenticated"}, RpcRefused),
    ],
)
async def test_error_mapping(reply, exc):
    nc = FakeNC([_desc(), reply, _desc()])
    c = OrdersClient(nc, service="orders")
    with pytest.raises(exc):
        await c.create(Order(sku="a"))


@pytest.mark.parametrize(
    ("kwargs", "sent"), [({}, 30.0), ({"timeout": 2.5}, 2.5)], ids=["default", "configured"]
)
async def test_every_request_is_given_the_clients_timeout(kwargs, sent):
    """nats-py's own default is 0.5 s, so a request sent without the client's
    timeout would give up early while the error still named the configured one."""
    nc = FakeNC([_desc(), {"success": True, "result": {"sku": "a", "qty": 1}}])
    c = OrdersClient(nc, service="orders", **kwargs)

    await c.create(Order(sku="a"))

    assert [s for s, _, _ in nc.sent] == ["orders.describe", "orders.rpc.create"]
    assert nc.timeouts == [sent, sent]


async def test_a_client_that_dials_its_own_connection_registers_its_reconnect_hook(
    monkeypatch,
):
    """Re-verification after a reconnect depends on nats-py calling the client's
    `_on_reconnect`, and nats-py takes that callback only when connecting."""

    dialled: list[dict] = []

    async def connect(*args, **kwargs):
        dialled.append(kwargs)
        return FakeNC([])

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)
    c = OrdersClient(nats_url="nats://example:4222", service="orders", verify=False)

    await c._connection()

    assert len(dialled) == 1
    assert dialled[0]["reconnected_cb"] == c._on_reconnect


async def test_CONTROL_a_client_given_a_connection_dials_nothing(monkeypatch):
    async def connect(*args, **kwargs):
        raise AssertionError("a client given a connection must not dial one")

    monkeypatch.setattr("cliffracer.core.dial.connect", connect)
    nc = FakeNC([])
    c = OrdersClient(nc, service="orders", verify=False)

    assert await c._connection() is nc


async def test_timeout_maps_to_RpcTimeout():
    import nats.errors

    nc = FakeNC([_desc(), nats.errors.TimeoutError()])
    c = OrdersClient(nc, service="orders", timeout=0.01)
    with pytest.raises(RpcTimeout):
        await c.create(Order(sku="a"))


async def test_a_reply_without_success_is_a_protocol_error():
    nc = FakeNC([_desc(), {"result": "x"}])
    c = OrdersClient(nc, service="orders")
    with pytest.raises(RpcServerError, match="protocol"):
        await c.create(Order(sku="a"))


async def test_verify_false_skips_the_describe_call():
    nc = FakeNC([{"success": True, "result": {"sku": "a", "qty": 1}}])
    c = OrdersClient(nc, service="orders", verify=False)
    await c.create(Order(sku="a"))
    assert nc.sent[0][0] == "orders.rpc.create"


async def test_a_client_that_declares_no_signatures_refuses_to_verify():
    """An empty SIGNATURES compares nothing, so verification is a no-op that
    reads as protection -- strictly worse than `verify=False`, which at least
    says so. `SIGNATURES` is the class default, so any hand-written subclass
    that omits it inherits the empty dict.

    The reply queued here advertises a different version and a different
    description hash and no methods at all: without the guard the client
    verifies against it happily. The guard must fire before the request goes
    out, so `nc.sent` is the assertion that separates refusing from
    round-tripping and discarding the answer.
    """

    class UndeclaredClient(ServiceClient):
        SERVICE = "orders"
        VERSION = "1"
        DESCRIPTION_HASH = "sha256:x"

    assert UndeclaredClient.SIGNATURES == {}

    nc = FakeNC([_desc(sig="sha256:TOTALLY-DIFFERENT", methods=())])
    c = UndeclaredClient(nc, service="orders")

    with pytest.raises(ClientError) as exc:
        await c.verify()

    assert "declares no signatures" in str(exc.value)
    assert "orders" in str(exc.value)
    assert nc.sent == [], "refused before the describe round-trip, not after it"


async def test_the_describe_request_carries_the_clients_headers():
    """Found end to end: a client with a token could not verify against a
    service behind AuthExtension, because only `_call` sent the headers. The
    describe subject runs through the same hook chain, so it is refused for the
    same reason and needs the same credentials."""
    nc = FakeNC([_desc(), {"success": True, "result": {"sku": "a", "qty": 1}}])
    c = OrdersClient(nc, service="orders", headers={"authorization": "bearer tok"})

    await c.create(Order(sku="a"))

    describe_subject, _, describe_headers = nc.sent[0]
    assert describe_subject == "orders.describe"
    assert describe_headers["authorization"] == "bearer tok"
    assert describe_headers["correlation_id"], "a describe with no id at all"
    assert describe_headers["X-Correlation-ID"] == describe_headers["correlation_id"]


async def test_verify_really_runs_once_across_calls():
    """`_verified` is what stops a describe on every call. Two calls, one describe."""
    nc = FakeNC(
        [
            _desc(),
            {"success": True, "result": {"sku": "a", "qty": 1}},
            {"success": True, "result": {"sku": "b", "qty": 1}},
        ]
    )
    c = OrdersClient(nc, service="orders")
    await c.create(Order(sku="a"))
    await c.create(Order(sku="b"))

    subjects = [s for s, _, _ in nc.sent]
    assert subjects == ["orders.describe", "orders.rpc.create", "orders.rpc.create"]


def _ids_sent(nc: FakeNC) -> list[tuple[str, str]]:
    """The two spellings of the correlation id each request carried, in the order they were sent."""
    return [(h["X-Correlation-ID"], h["correlation_id"]) for _, _, h in nc.sent]


def _a_describe_and_two_calls(**client_kwargs) -> tuple[FakeNC, OrdersClient]:
    nc = FakeNC(
        [
            _desc(),
            {"success": True, "result": {"sku": "a", "qty": 1}},
            {"success": True, "result": {"sku": "b", "qty": 1}},
        ]
    )
    return nc, OrdersClient(nc, service="orders", **client_kwargs)


async def test_the_describe_and_each_call_carry_a_correlation_id_of_their_own():
    """With no id set by the caller and none ambient, no two requests share one.

    The source says ids are generated per request "so describe calls and subsequent RPC
    invocations maintain distinct tracing identifiers". Checked on the helper alone, that
    leaves the two places that send: a call that sent the describe's headers again would pass
    every test that reads one request, or only whether an id is present.
    """
    from cliffracer.core.correlation import correlation_id_var

    assert correlation_id_var.get() is None, "an ambient id would make this a different test"
    nc, c = _a_describe_and_two_calls()

    await c.create(Order(sku="a"))
    await c.create(Order(sku="b"))

    sent = _ids_sent(nc)
    assert [s for s, _, _ in nc.sent] == [
        "orders.describe",
        "orders.rpc.create",
        "orders.rpc.create",
    ]
    assert all(canonical and canonical == legacy for canonical, legacy in sent), sent
    assert len({canonical for canonical, _ in sent}) == 3, sent


async def test_an_id_the_caller_set_is_the_id_on_the_describe_and_on_every_call():
    """An explicit id is propagated, not only present: it is the one on each request, verbatim,
    in both header spellings, and the caller's other headers are still sent with it."""
    nc, c = _a_describe_and_two_calls(
        headers={"correlation_id": "fixed-trace", "authorization": "bearer tok"}
    )

    await c.create(Order(sku="a"))
    await c.create(Order(sku="b"))

    assert _ids_sent(nc) == [("fixed-trace", "fixed-trace")] * 3
    assert {h["authorization"] for _, _, h in nc.sent} == {"bearer tok"}


async def test_encode_uses_the_declared_annotation_not_the_runtime_type():
    """The reason `_encode` takes an annotation at all.

    `TypeAdapter(type(value))` on a list of models sees `list` and dumps the
    models as objects it does not know how to serialise. The declared
    annotation keeps the item type, which is what the generated stub passes.
    """
    c = OrdersClient(FakeNC([]), service="orders")

    assert c._encode([Order(sku="a", qty=2)], list[Order]) == [{"sku": "a", "qty": 2}]
    assert c._encode(None, Order | None) is None
    assert c._encode({"x": Order(sku="b")}, dict[str, Order]) == {"x": {"sku": "b", "qty": 1}}


# --- describe subject envelope handling -------------------------------------


async def _refusal_envelope_from_the_container() -> dict:
    """The bytes `container.py` actually writes when an extension refuses.

    Not a hand-written dict. The client's job is to read what the server
    sends, so the fixture is produced by the server: a service whose extension
    raises `RejectMessage` on a describe, dispatched through the real
    `_handle_describe_request`.
    """
    from cliffracer import CliffracerService, ServiceConfig
    from cliffracer.core.extension import Extension, RejectMessage

    class Deny(Extension):
        async def worker_setup(self, ctx):
            if ctx.kind == "describe":
                raise RejectMessage("unauthenticated")

    class Guarded(CliffracerService):
        deny = Deny()

        @rpc
        async def create(self, order: Order) -> Order:
            return order

    class _Msg:
        #: Every dispatcher path reads this; a double without one let a
        #: reply be recorded that production would have refused.
        reply: str | None = "_INBOX.test"

        def __init__(self):
            self.subject = "orders.describe"
            self.data = b""
            self.headers: dict[str, str] = {}
            self.response: dict | None = None

        async def respond(self, payload: bytes) -> None:
            refuse_a_reply_with_no_subject(self)
            self.response = json.loads(payload.decode())

    svc = Guarded(ServiceConfig(name="orders"))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    msg = _Msg()
    await svc.container._handle_describe_request(msg)
    return msg.response


async def test_a_refused_describe_is_RpcRefused_not_a_KeyError():
    """Verify refused describe request raises RpcRefused rather than KeyError."""
    envelope = await _refusal_envelope_from_the_container()
    assert envelope["error"].startswith("refused: "), envelope

    c = OrdersClient(FakeNC([envelope]), service="orders")
    with pytest.raises(RpcRefused) as caught:
        await c.create(Order(sku="a"))
    assert caught.value.reason == "unauthenticated"


async def test_a_describe_that_errors_is_a_server_error_naming_the_subject():
    c = OrdersClient(FakeNC([{"error": "boom", "timestamp": "t"}]), service="orders")
    with pytest.raises(RpcServerError) as caught:
        await c.verify()
    assert "orders.describe" in str(caught.value) and "boom" in str(caught.value)


async def test_a_describe_timeout_is_RpcTimeout():
    """The existing timeout test cannot reach this: its fake hands verify a
    good description first, so the timeout it scripts always lands on the CALL.
    Here the FIRST reply is the timeout, which is the describe."""
    import nats.errors

    c = OrdersClient(FakeNC([nats.errors.TimeoutError()]), service="orders", timeout=0.01)
    with pytest.raises(RpcTimeout) as caught:
        await c.create(Order(sku="a"))
    assert "orders.describe" in str(caught.value)


@pytest.mark.parametrize("verify", [True, False])
async def test_no_responders_is_its_own_error_on_both_paths(verify):
    """A stopped service is not a timeout: the broker says so immediately, and
    that is the diagnostic that separates 'nobody is running it' from 'it did
    not answer in time'."""
    import nats.errors

    c = OrdersClient(FakeNC([nats.errors.NoRespondersError()]), service="orders", verify=verify)
    with pytest.raises(RpcNoResponders) as caught:
        await c.create(Order(sku="a"))
    expected = "orders.describe" if verify else "orders.rpc.create"
    assert expected in str(caught.value)


async def test_a_description_for_another_service_is_refused_by_name():
    """A `service=` or `namespace=` slip points the client at the wrong
    service. The hashes can line up by coincidence of shape, so the name is
    checked rather than left to them."""
    other = {**_desc(), "service": "invoices"}
    c = OrdersClient(FakeNC([other]), service="orders")
    with pytest.raises(ClientError) as caught:
        await c.verify()
    assert "invoices" in str(caught.value) and "orders" in str(caught.value)


# --- the base class, and the attribute it brought with it ---------------------


def test_the_client_errors_are_cliffracer_errors_but_not_service_errors():
    """`ClientError` under `CliffracerError`, deliberately not under
    `ServiceError`. A service catching `ServiceError` is catching "something
    went wrong while I handled a message"; a client call is not that, and
    widening it silently would be the kind of change nobody notices until an
    unrelated except clause starts swallowing client failures."""
    from cliffracer.core.exceptions import CliffracerError, RpcError, ServiceError

    assert issubclass(ClientError, CliffracerError)
    assert not issubclass(ClientError, ServiceError)
    assert issubclass(ClientError, RpcError)
    for cls in (
        RpcValidationError,
        RpcUnknownMethod,
        RpcRefused,
        RpcTimeout,
        RpcNoResponders,
        ClientOutOfDate,
    ):
        assert issubclass(cls, ClientError), cls


def test_a_validation_error_prints_pydantics_list_once():
    """Verify RpcValidationError formats details without duplication."""
    details = [{"loc": ["qty"], "msg": "must be > 0"}]

    error = RpcValidationError(details)

    assert str(error) == "validation failed - Details: [{'loc': ['qty'], 'msg': 'must be > 0'}]"
    assert error.details == details
    assert isinstance(error.details, list)


async def test_re_verify_on_validation_error_detects_replica_mismatch():
    v1_desc = _desc(sig="sha256:sig-create")
    v2_desc = _desc(sig="sha256:sig-create-v2")
    val_err_reply = {
        "success": False,
        "error": "validation failed",
        "details": [{"loc": ["rush"], "msg": "Field required"}],
        "timestamp": "t",
        "correlation_id": "c",
    }
    nc = FakeNC([v1_desc, val_err_reply, v2_desc])
    c = OrdersClient(nc, service="orders", namespace="t1")
    with pytest.raises(ClientOutOfDate) as exc:
        await c.create(Order(sku="a", qty=2))
    assert exc.value.changed == ["create"]


async def test_re_verify_on_validation_error_re_raises_when_schema_unchanged():
    v1_desc = _desc(sig="sha256:sig-create")
    val_err_reply = {
        "success": False,
        "error": "validation failed",
        "details": [{"loc": ["qty"], "msg": "Field required"}],
        "timestamp": "t",
        "correlation_id": "c",
    }
    nc = FakeNC([v1_desc, val_err_reply, v1_desc])
    c = OrdersClient(nc, service="orders", namespace="t1")
    with pytest.raises(RpcValidationError):
        await c.create(Order(sku="a", qty=2))
