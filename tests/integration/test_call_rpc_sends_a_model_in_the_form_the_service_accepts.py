"""A model passed to `call_rpc`, `RpcProxy`, `call_rpc_no_wait` or `call_async` is accepted by a live service.

These calls know no handler annotation: the arguments are `**kwargs`. They send a model by
alias, as `to_jsonable_python` does, and a model the service reads by field name (a
`validate_by_alias=False` model, or one whose `serialization_alias` differs from its
`validation_alias`) was refused. Each model is now sent in the form its own class reads back
as the argument, alias first, so what was accepted goes out byte for byte as before.

The bytes the service was sent are read off the subject, and the service's reply or the value its
handler recorded is the evidence that it accepted them.
"""

import asyncio
from datetime import UTC, datetime
from decimal import Decimal
from uuid import UUID

import pytest
from pydantic import BaseModel, ConfigDict, Field
from pydantic.alias_generators import to_camel

from cliffracer import CliffracerService, RpcProxy, ServiceConfig, async_rpc, rpc
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import deserialize_payload

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

CALLEE = "shop_call_rt"


class ByNameOnly(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    item_name: str = Field(alias="itemName")


class FrozenByName(BaseModel):
    """Hashable, so a set can hold it."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True, frozen=True)
    item_name: str = Field(alias="itemName")


class SplitAliases(BaseModel):
    model_config = ConfigDict(validate_by_name=True)
    item_name: str = Field(validation_alias="item_in", serialization_alias="itemOut")


class AliasOnly(BaseModel):
    item_name: str = Field(alias="itemName")


class Populatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    item_name: str = Field(alias="itemName")


class Camel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel)
    item_name: str


class Inner(BaseModel):
    inner_name: str = Field(alias="innerName")


class Outer(BaseModel):
    outer_name: str = Field(alias="outerName")
    inner: Inner


class Swapped(BaseModel):
    a: str = Field(alias="b")
    b: str = Field(alias="a")


class Base(BaseModel):
    x: int = Field(alias="xx")


class SubByName(Base):
    """The handler declares `Base`; the caller passes this, read by field name only."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class BasePopulatable(BaseModel):
    model_config = ConfigDict(populate_by_name=True)
    x: int = Field(alias="xx")


class SubPopulatableByName(BasePopulatable):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class AppModel(BaseModel):
    """What a project puts at the top of its models: configuration and no fields."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class ByNameOverAppModel(AppModel):
    item_name: str = Field(alias="itemName")


class WithDefaults(BaseModel):
    note: str = "n"


class ByNameOverDefaults(WithDefaults):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    item_name: str = Field(alias="itemName")


class Level1(BaseModel):
    x: int = Field(alias="xx")


class Level2(Level1):
    model_config = ConfigDict(populate_by_name=True)


class Level3(Level2):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)


class ByNameWithADefault(BaseModel):
    """The alias form is accepted by this class and read as the default."""

    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    x: int = Field(7, alias="xx")


class ByNameSwapped(BaseModel):
    model_config = ConfigDict(validate_by_alias=False, validate_by_name=True)
    a: str = Field(alias="b")
    b: str = Field(alias="a")


class StrictByName(BaseModel):
    """Strict and read by field name: its datetime, UUID, Decimal and tuple fields are JSON text and
    arrays on the wire, which python mode refuses and JSON mode reads."""

    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)
    when: datetime = Field(alias="When")
    ident: UUID = Field(alias="Ident")
    amount: Decimal = Field(alias="Amount")
    pair: tuple[int, int] = Field(alias="Pair")


class StrictAppModel(BaseModel):
    model_config = ConfigDict(strict=True, validate_by_alias=False, validate_by_name=True)


class StrictByNameOverAppModel(StrictAppModel):
    when: datetime = Field(alias="When")
    ident: UUID = Field(alias="Ident")


STRICT_WHEN = datetime(2026, 1, 2, 3, 4, 5, tzinfo=UTC)
STRICT_IDENT = UUID("12345678-1234-5678-1234-567812345678")


class Shop(CliffracerService):
    def __init__(self, config: ServiceConfig) -> None:
        super().__init__(config)
        self.recorded: list[str] = []

    @rpc
    async def by_name_only(self, item: ByNameOnly) -> str:
        self.recorded.append(item.item_name)
        return item.item_name

    @async_rpc
    async def by_name_only_async(self, item: ByNameOnly) -> None:
        self.recorded.append(item.item_name)

    @rpc
    async def base(self, item: Base) -> str:
        self.recorded.append(str(item.x))
        return str(item.x)

    @async_rpc
    async def base_async(self, item: Base) -> None:
        self.recorded.append(str(item.x))

    @rpc
    async def base_populatable(self, item: BasePopulatable) -> str:
        return str(item.x)

    @rpc
    async def app_model(self, item: ByNameOverAppModel) -> str:
        return item.item_name

    @rpc
    async def with_defaults(self, item: ByNameOverDefaults) -> str:
        return item.item_name

    @rpc
    async def level1(self, item: Level1) -> str:
        return str(item.x)

    @rpc
    async def declared_subclass(self, item: SubByName) -> str:
        return str(item.x)

    @rpc
    async def by_name_with_a_default(self, item: ByNameWithADefault) -> str:
        return str(item.x)

    @rpc
    async def by_name_swapped(self, item: ByNameSwapped) -> str:
        return f"{item.a}/{item.b}"

    @rpc
    async def strict_by_name(self, item: StrictByName) -> str:
        return f"{item.when.isoformat()}|{item.ident}|{item.amount}|{item.pair}"

    @rpc
    async def strict_over_app_model(self, item: StrictByNameOverAppModel) -> str:
        return f"{item.when.isoformat()}|{item.ident}"

    @rpc
    async def split_aliases(self, item: SplitAliases) -> str:
        return item.item_name

    @rpc
    async def listed(self, items: list[ByNameOnly]) -> str:
        return ",".join(i.item_name for i in items)

    @rpc
    async def listed_frozen(self, items: list[FrozenByName]) -> str:
        return ",".join(i.item_name for i in items)

    @rpc
    async def mapped(self, items: dict[str, ByNameOnly]) -> str:
        return ",".join(f"{k}={v.item_name}" for k, v in items.items())

    @rpc
    async def alias_only(self, item: AliasOnly) -> str:
        return item.item_name

    @rpc
    async def populatable(self, item: Populatable) -> str:
        return item.item_name

    @rpc
    async def camel(self, item: Camel) -> str:
        return item.item_name

    @rpc
    async def nested(self, item: Outer) -> str:
        return f"{item.outer_name}/{item.inner.inner_name}"

    @rpc
    async def swapped(self, item: Swapped) -> str:
        return f"{item.a}/{item.b}"

    @rpc
    async def plain(self, tags: list[str], count: int, ratio: float) -> str:
        return f"{','.join(tags)}|{count}|{ratio}"


class Caller(CliffracerService):
    shop = RpcProxy(CALLEE)


# Seconds to wait for the observer's copy of a request, or for a handler with no reply to run.
# Generous, because a loaded host delays delivery; a run that passes waits only as long as it takes.
OBSERVER_TIMEOUT = 10.0
# Seconds to wait after the copy arrives before counting copies, so a second one would be counted.
SETTLE = 0.1


async def _until(condition, timeout: float) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not condition():
        if asyncio.get_running_loop().time() > deadline:
            raise AssertionError(f"not true within {timeout} s")
        await asyncio.sleep(0.01)


async def _send(nats_connection, how, method, fmt="json", **kwargs):
    """Make one call the way `how` says; return (the reply or the refusal, the JSON body sent).

    The observer subscribes, and is flushed to the broker, before either service starts, and the
    call waits on its callback rather than polling for a copy.
    """
    callee = Shop(ServiceConfig(name=CALLEE, version="1.0.0"))
    caller = Caller(ServiceConfig(name="caller_call_rt", version="1.0.0", serialization_format=fmt))
    verb = "async" if how == "call_async" else "rpc"
    subject = HandlerDiscovery.outbound_subject(caller.config, CALLEE, verb, method)
    seen: list[bytes] = []
    arrived = asyncio.Event()

    async def record(msg):
        seen.append(msg.data)
        arrived.set()

    sub = await nats_connection.subscribe(subject, cb=record)
    await nats_connection.flush()
    await callee.start()
    await caller.start()
    try:
        try:
            if how == "call_rpc":
                reply = await caller.call_rpc(CALLEE, method, **kwargs)
            elif how == "proxy":
                reply = await getattr(caller.shop, method)(**kwargs)
            elif how == "call_rpc_no_wait":
                reply = await caller.call_rpc_no_wait(CALLEE, method, **kwargs)
            else:
                reply = await caller.call_async(CALLEE, method, **kwargs)
        except RpcValidationError as exc:
            reply = exc
        await asyncio.wait_for(arrived.wait(), timeout=OBSERVER_TIMEOUT)
        if how not in ("call_rpc", "proxy"):
            await _until(lambda: callee.recorded, OBSERVER_TIMEOUT)
        await asyncio.sleep(SETTLE)
        assert len(seen) == 1, seen
        body = deserialize_payload(seen[0], content_type=None, fallback_format=fmt)
        body.pop("correlation_id", None)
        return reply, body, callee
    finally:
        await sub.unsubscribe()
        await caller.stop()
        await callee.stop()


@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
@pytest.mark.parametrize(
    ("method", "kwargs", "reply", "wire"),
    [
        pytest.param(
            "by_name_only",
            {"item": ByNameOnly(item_name="x")},
            "x",
            {"item": {"item_name": "x"}},
            id="validate_by_alias-off",
        ),
        pytest.param(
            "split_aliases",
            {"item": SplitAliases(item_name="x")},
            "x",
            {"item": {"item_name": "x"}},
            id="split-aliases",
        ),
        pytest.param(
            "listed",
            {"items": [ByNameOnly(item_name="a"), ByNameOnly(item_name="b")]},
            "a,b",
            {"items": [{"item_name": "a"}, {"item_name": "b"}]},
            id="list",
        ),
        pytest.param(
            "listed",
            {"items": (ByNameOnly(item_name="a"), ByNameOnly(item_name="b"))},
            "a,b",
            {"items": [{"item_name": "a"}, {"item_name": "b"}]},
            id="tuple",
        ),
        pytest.param(
            "listed_frozen",
            {"items": {FrozenByName(item_name="a")}},
            "a",
            {"items": [{"item_name": "a"}]},
            id="set",
        ),
        pytest.param(
            "mapped",
            {"items": {"k": ByNameOnly(item_name="a")}},
            "k=a",
            {"items": {"k": {"item_name": "a"}}},
            id="dict",
        ),
    ],
)
async def test_a_model_the_service_reads_by_field_name_is_sent_by_field_name(
    nats_connection, how, method, kwargs, reply, wire
):
    got, sent, _ = await _send(nats_connection, how, method, **kwargs)

    assert got == reply
    assert sent == wire


@pytest.mark.parametrize("how", ["call_rpc_no_wait", "call_async"])
async def test_the_calls_with_no_reply_send_the_same_form(nats_connection, how):
    method = "by_name_only" if how == "call_rpc_no_wait" else "by_name_only_async"

    _, sent, callee = await _send(nats_connection, how, method, item=ByNameOnly(item_name="x"))

    assert sent == {"item": {"item_name": "x"}}
    assert callee.recorded == ["x"]


@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
@pytest.mark.parametrize(
    ("method", "kwargs", "reply", "wire"),
    [
        pytest.param(
            "alias_only",
            {"item": AliasOnly(itemName="x")},
            "x",
            {"item": {"itemName": "x"}},
            id="alias-only",
        ),
        pytest.param(
            "populatable",
            {"item": Populatable(itemName="x")},
            "x",
            {"item": {"itemName": "x"}},
            id="populatable",
        ),
        pytest.param(
            "camel", {"item": Camel(itemName="x")}, "x", {"item": {"itemName": "x"}}, id="generator"
        ),
        pytest.param(
            "nested",
            {"item": Outer(outerName="o", inner=Inner(innerName="i"))},
            "o/i",
            {"item": {"outerName": "o", "inner": {"innerName": "i"}}},
            id="nested",
        ),
        pytest.param(
            "swapped",
            {"item": Swapped(b="1", a="2")},
            "1/2",
            {"item": {"b": "1", "a": "2"}},
            id="swapped-aliases",
        ),
    ],
)
async def test_a_call_that_was_accepted_is_sent_the_bytes_it_was_sent_before(
    nats_connection, how, method, kwargs, reply, wire
):
    got, sent, _ = await _send(nats_connection, how, method, **kwargs)

    assert got == reply
    assert sent == wire


async def test_arguments_that_are_not_models_are_sent_as_before(nats_connection):
    got, sent, _ = await _send(
        nats_connection, "call_rpc", "plain", tags=("b", "a"), count=3, ratio=0.5
    )

    assert got == "b,a|3|0.5"
    assert sent == {"tags": ["b", "a"], "count": 3, "ratio": 0.5}


@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
@pytest.mark.parametrize(
    ("method", "item"),
    [
        pytest.param("base", SubByName(x=1), id="alias-only-base"),
        pytest.param("level1", Level3(x=1), id="three-levels"),
    ],
)
async def test_a_subclass_passed_to_a_handler_that_declares_its_base_is_sent_as_before(
    nats_connection, how, method, item
):
    """The handler reads the alias form main sent. The subclass refuses it, and the call still
    goes out in it because the base class the handler declares reads it back."""
    got, sent, _ = await _send(nats_connection, how, method, item=item)

    assert got == "1"
    assert sent == {"item": {"xx": 1}}


async def test_a_base_that_reads_either_form_is_delivered_by_field_name(nats_connection):
    """`BasePopulatable` reads both forms, so it does not decide: the subclass that refuses the
    alias form does, and the handler declaring the base reads the field-name form too."""
    got, sent, _ = await _send(
        nats_connection, "call_rpc", "base_populatable", item=SubPopulatableByName(x=1)
    )

    assert got == "1"
    assert sent == {"item": {"x": 1}}


async def test_a_subclass_passed_to_a_handler_that_declares_the_subclass_is_refused(
    nats_connection,
):
    """The remaining limit. The base reads only the alias form, and a call has no annotation
    to say that the handler declared the subclass, which reads only the field-name form: the
    alias form, which the base reads and the call sent before, is kept, and the handler refuses
    it."""
    got, sent, _ = await _send(
        nats_connection, "call_rpc", "declared_subclass", item=SubByName(x=1)
    )

    assert isinstance(got, RpcValidationError), got
    assert sent == {"item": {"xx": 1}}


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
@pytest.mark.parametrize(
    ("method", "item", "wire"),
    [
        pytest.param(
            "app_model",
            ByNameOverAppModel(item_name="x"),
            {"item_name": "x"},
            id="base-with-only-configuration",
        ),
        pytest.param(
            "with_defaults",
            ByNameOverDefaults(item_name="x"),
            {"note": "n", "item_name": "x"},
            id="base-with-only-defaulted-fields",
        ),
    ],
)
async def test_a_base_that_cannot_tell_the_forms_apart_does_not_stop_the_field_name_form(
    nats_connection, how, fmt, method, item, wire
):
    got, sent, _ = await _send(nats_connection, how, method, fmt=fmt, item=item)

    assert got == "x"
    assert sent == {"item": wire}


@pytest.mark.parametrize("how", ["call_rpc_no_wait", "call_async"])
async def test_the_calls_with_no_reply_deliver_a_subclass_to_a_handler_that_declares_its_base(
    nats_connection, how
):
    method = "base" if how == "call_rpc_no_wait" else "base_async"

    _, sent, callee = await _send(nats_connection, how, method, item=SubByName(x=1))

    assert sent == {"item": {"xx": 1}}
    assert callee.recorded == ["1"]


@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
async def test_a_model_the_service_reads_by_field_name_is_accepted_in_msgpack(nats_connection, how):
    got, sent, _ = await _send(
        nats_connection, how, "by_name_only", fmt="msgpack", item=ByNameOnly(item_name="x")
    )

    assert got == "x"
    assert sent == {"item": {"item_name": "x"}}


@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
@pytest.mark.parametrize(
    ("method", "item", "reply"),
    [
        pytest.param("by_name_with_a_default", ByNameWithADefault(x=1), "1", id="default"),
        pytest.param("by_name_swapped", ByNameSwapped(a="1", b="2"), "1/2", id="swapped"),
    ],
)
async def test_the_service_reads_the_value_that_was_passed_not_one_the_alias_form_reads_as(
    nats_connection, how, method, item, reply
):
    """The alias form of each is accepted by the service and read as another value: the default
    7, or the two values exchanged. The reply is the value the handler read."""
    got, sent, _ = await _send(nats_connection, how, method, item=item)

    assert got == reply
    assert "xx" not in sent["item"]


@pytest.mark.parametrize("fmt", ["json", "msgpack"])
@pytest.mark.parametrize("how", ["call_rpc", "proxy"])
@pytest.mark.parametrize(
    ("method", "item", "reply", "wire"),
    [
        pytest.param(
            "strict_by_name",
            StrictByName(when=STRICT_WHEN, ident=STRICT_IDENT, amount=Decimal("1.50"), pair=(1, 2)),
            "2026-01-02T03:04:05+00:00|12345678-1234-5678-1234-567812345678|1.50|(1, 2)",
            {
                "when": "2026-01-02T03:04:05Z",
                "ident": "12345678-1234-5678-1234-567812345678",
                "amount": "1.50",
                "pair": [1, 2],
            },
            id="strict-by-name",
        ),
        pytest.param(
            "strict_over_app_model",
            StrictByNameOverAppModel(when=STRICT_WHEN, ident=STRICT_IDENT),
            "2026-01-02T03:04:05+00:00|12345678-1234-5678-1234-567812345678",
            {"when": "2026-01-02T03:04:05Z", "ident": "12345678-1234-5678-1234-567812345678"},
            id="strict-app-model-subclass",
        ),
    ],
)
async def test_a_strict_model_read_by_field_name_is_delivered(
    nats_connection, how, fmt, method, item, reply, wire
):
    """Each form is read the way the service reads it, python mode then JSON mode, so the JSON text
    of a datetime, UUID or Decimal and the array of a tuple are read by the strict model, and the
    field-name form is chosen and accepted."""
    got, sent, _ = await _send(nats_connection, how, method, fmt=fmt, item=item)

    assert got == reply
    assert sent == {"item": wire}
