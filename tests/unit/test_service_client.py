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
    RpcTimeout,
    RpcUnknownMethod,
    RpcValidationError,
    ServiceClient,
)

pytestmark = pytest.mark.unit


class Order(BaseModel):
    sku: str
    qty: int = 1


class FakeNC:
    def __init__(self, replies):
        self.replies = list(replies)
        self.sent = []
        self.is_closed = False
        self._cbs = []

    async def request(self, subject, payload, timeout=None, headers=None):
        self.sent.append((subject, json.loads(payload or b"{}"), headers))
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
    assert headers["authorization"] == "bearer tok" and "correlation_id" in headers


async def test_verify_runs_once_and_names_changed_and_missing_methods():
    nc = FakeNC([_desc(sig="sha256:OTHER", methods=())])
    c = OrdersClient(nc, service="orders")
    with pytest.raises(ClientOutOfDate) as e:
        await c.create(Order(sku="a"))
    assert e.value.missing == ["create"]


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


async def test_timeout_maps_to_RpcTimeout():
    import nats.errors

    nc = FakeNC([_desc(), nats.errors.TimeoutError()])
    c = OrdersClient(nc, service="orders", timeout=0.01)
    with pytest.raises(RpcTimeout):
        await c.create(Order(sku="a"))


async def test_a_reply_without_success_is_a_protocol_error():
    nc = FakeNC([_desc(), {"result": "x"}])
    c = OrdersClient(nc, service="orders")
    with pytest.raises(ClientError, match="protocol"):
        await c.create(Order(sku="a"))


async def test_verify_false_skips_the_describe_call():
    nc = FakeNC([{"success": True, "result": {"sku": "a", "qty": 1}}])
    c = OrdersClient(nc, service="orders", verify=False)
    await c.create(Order(sku="a"))
    assert nc.sent[0][0] == "orders.rpc.create"


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
    assert "correlation_id" in describe_headers


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
        def __init__(self):
            self.subject = "orders.describe"
            self.data = b""
            self.headers: dict[str, str] = {}
            self.response: dict | None = None

        async def respond(self, payload: bytes) -> None:
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


async def test_a_describe_that_errors_is_a_ClientError_naming_the_subject():
    c = OrdersClient(FakeNC([{"error": "boom", "timestamp": "t"}]), service="orders")
    with pytest.raises(ClientError) as caught:
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
