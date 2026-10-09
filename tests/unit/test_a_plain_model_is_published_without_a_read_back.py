"""A plain model is written without validating its dump back, and nothing it sends changes.

`wire_models` validates each model it writes to learn whether its class reads the alias dump back as
the value. A plain class (no alias, validator or serializer, inert config, fields of the plain
types) has one form, so the dump is sent without that validation when the instance also holds
exactly its fields' types. The property check runs every generated instance through `wire_models`
twice, with the skip and without it, and requires the same form or the same refusal. Two CONTROLs
show it can fail. Without the config check, a class for each near-miss config key is run every time,
and the one that changes which form a class reads (an alias generator read by field name) changes an
outcome. Without the walk's test of a datetime's or time's zone, the seeded cases find outcomes that
change, at least the measured floor. The zone test turns away the values the read-back refuses (an
offset finer than a minute, a `ZoneInfo` time in a gap, a time its `ZoneInfo` gives no offset).
Keeping `bytes` out of a two-type union decides an outcome too: `str | bytes` reads bytes back as
`str`, which main refuses. The rest of the walk turns away values no outcome tells apart today (a
subclass in a nested field, a NaN, a bool for an int, a datetime for a date, a temporal subclass, an
int or a str for a `Decimal`, another enum's member, bytes that are not UTF-8, a value neither type
of a union takes, a missing field, a lone surrogate), so it is held by what it returns for each, not
by an outcome.
"""

from __future__ import annotations

import enum
import math
import random
import weakref
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Annotated, Any
from zoneinfo import ZoneInfo

import annotated_types
import pytest
from pydantic import (
    AwareDatetime,
    BaseModel,
    ConfigDict,
    Field,
    FutureDate,
    FutureDatetime,
    NaiveDatetime,
    PastDate,
    PastDatetime,
    RootModel,
    ValidationError,
    computed_field,
    create_model,
    field_validator,
)
from pydantic.fields import FieldInfo

from cliffracer.core import validation
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import wire_models
from tests.fixtures.properties import (
    Finding,
    assert_control_finds,
    assert_only_known_limits,
    cases,
    seeds,
)
from tests.fixtures.properties.plain_models import (
    NEAR_MISS_CONFIGS,
    generate,
    near_miss_with_config,
)

pytestmark = pytest.mark.unit

SEED = 2451
CASES = 600
#: The config keys the config CONTROL runs a near miss for, one class each, every run. The seeded
#: cases draw these too, but which a seed draws moves with every generator change; the control
#: asserts each key is present rather than a measured count.
CONFIG_KEYS_RUN = frozenset(
    {
        frozenset({"str_strip_whitespace"}),
        frozenset({"str_to_lower"}),
        frozenset({"str_to_upper"}),
        frozenset({"str_max_length"}),
        frozenset({"coerce_numbers_to_str"}),
        frozenset({"strict"}),
        frozenset({"ser_json_inf_nan"}),
        frozenset({"ser_json_bytes"}),
        frozenset({"ser_json_timedelta"}),
        frozenset({"serialize_by_alias"}),
        frozenset({"alias_generator", "populate_by_name"}),
        frozenset({"alias_generator", "validate_by_alias", "validate_by_name"}),
        frozenset({"extra"}),
    }
)


def _outcome(value) -> tuple:
    try:
        return ("sent", repr(wire_models({"item": value})))
    except Exception as exc:
        return ("refused", type(exc).__name__, str(exc))


def findings(
    monkeypatch, count: int = CASES, values: list | None = None
) -> tuple[list[Finding], int]:
    """Instances whose outcome the skip changes, and how many the skip took. `values`, when given,
    receives each such instance, in the order of the findings."""
    found: list[Finding] = []
    skipped = 0
    for seed in seeds(SEED):
        rng = random.Random(seed)
        for index in range(cases(count)):
            value, described = generate(rng, index)
            took = validation._is_plain_instance(value)
            skipped += took
            with_skip = _outcome(value)
            with monkeypatch.context() as off:
                off.setattr(validation, "_is_plain_instance", lambda v: False)
                without = _outcome(value)
            if with_skip != without:
                found.append(
                    Finding(
                        seed,
                        index,
                        f"the skip changed the outcome: {with_skip} instead of {without}",
                        described,
                        took,
                    )
                )
                if values is not None:
                    values.append(value)
    return found, skipped


def test_skipping_the_read_back_changes_no_form_and_no_refusal(monkeypatch):
    found, skipped = findings(monkeypatch)

    assert skipped > 0, "no generated instance took the skip, so the check compares nothing"
    assert_only_known_limits(found, [], check="the plain-model skip")


def _fresh_cache(monkeypatch):
    monkeypatch.setattr(validation, "_plain_classes", weakref.WeakKeyDictionary())


def test_CONTROL_a_class_test_without_the_config_check_changes_outcomes(monkeypatch):
    """Without the config check, each near-miss config's class is taken as plain. Each key is run
    once, and only the alias generator that reads by field name changes an outcome: the string
    transforms, length limits, strict and the rest leave main sending the same dump, since what
    they change is normalisation, not a lost value."""
    _fresh_cache(monkeypatch)
    every_key = frozenset(validation._config_defaults or ()) | {"extra"}
    monkeypatch.setattr(validation, "_INERT_CONFIG", every_key)

    changed: dict[frozenset[str], bool] = {}
    for config in NEAR_MISS_CONFIGS:
        value, _ = near_miss_with_config(config)
        with_skip = _outcome(value)
        with monkeypatch.context() as off:
            off.setattr(validation, "_is_plain_instance", lambda v: False)
            without = _outcome(value)
        changed[frozenset(config)] = with_skip != without

    assert set(changed) == CONFIG_KEYS_RUN, sorted(map(sorted, set(changed) ^ CONFIG_KEYS_RUN))
    assert [sorted(keys) for keys, moved in changed.items() if moved] == [
        ["alias_generator", "validate_by_alias", "validate_by_name"]
    ], changed


#: The kinds of zoned value the read-back refuses, each of which the zone CONTROL must find: the
#: generator draws all three, so a run that finds none of one has lost either the case or the
#: refusal. A `ZoneInfo` datetime in a repeated hour is read back as the same instant and sent
#: either way, so it is not one.
ZONE_KINDS = frozenset(
    {
        "an offset finer than a minute",
        "a wall time its zone does not have",
        "a time whose zone gives no offset",
    }
)


def _skipped(value: datetime) -> bool:
    """Whether `value`'s wall time does not survive a round trip through UTC in its own zone,
    worked out here rather than by the code under test, so a CONTROL against that code cannot
    blind its own classification."""
    back = value.astimezone(UTC).astimezone(value.tzinfo)
    return back.replace(tzinfo=None, fold=0) != value.replace(tzinfo=None, fold=0)


def _zone_kinds(value) -> set[str]:
    """The kinds of zoned value `value` holds, at any depth."""
    kinds: set[str] = set()
    if isinstance(value, BaseModel):
        for name in type(value).model_fields:
            kinds |= _zone_kinds(getattr(value, name, None))
    elif isinstance(value, dict):
        for item in value.values():
            kinds |= _zone_kinds(item)
    elif isinstance(value, list | tuple | set | frozenset):
        for item in value:
            kinds |= _zone_kinds(item)
    elif isinstance(value, datetime | time) and value.tzinfo is not None:
        offset = value.utcoffset()
        if offset is None:
            kinds.add("a time whose zone gives no offset")
        elif offset.microseconds or offset.seconds % 60:
            kinds.add("an offset finer than a minute")
        elif isinstance(value, datetime) and _skipped(value):
            kinds.add("a wall time its zone does not have")
    return kinds


def test_CONTROL_a_walk_without_the_zone_test_changes_outcomes(monkeypatch):
    """Without the zone test every zoned value is taken as plain and sent; the read-back refuses
    each kind of zoned value the wire cannot carry, so each kind must turn up as a finding. A
    presence check per kind, not a measured count: which seeded cases hold which zone moves with
    every generator change, and the count is reported, not asserted."""
    _fresh_cache(monkeypatch)
    monkeypatch.setattr(validation, "_has_a_whole_minute_offset_or_none", lambda value: True)

    values: list = []
    found, _ = findings(monkeypatch, values=values)

    assert_control_finds(found, at_least=1, control="no zone test")
    _assert_each_finding_holds_a_zone_and_each_kind_is_found(found, values)
    # Each is a value the read-back refuses: sent by the skip, refused without it.
    assert all("'refused'" in f.what.split(" instead of ")[1] for f in found), [
        f.what for f in found
    ]


def _assert_each_finding_holds_a_zone_and_each_kind_is_found(found, values) -> None:
    """Fail, with the count and the per-kind tally, on a finding that holds no zoned value (named
    by its reproduction) or on a kind that no finding holds."""
    tally = dict.fromkeys(sorted(ZONE_KINDS), 0)
    unexplained = []
    for finding, value in zip(found, values, strict=True):
        kinds = _zone_kinds(value)
        if not kinds:
            unexplained.append(finding.reproduction)
        for kind in kinds:
            tally[kind] += 1
    said = f"{len(found)} finding(s): {tally}"
    assert not unexplained, f"{said}; holding no zoned value the read-back refuses: {unexplained}"
    assert all(tally.values()), f"{said}; a kind the read-back refuses was not found"


def test_CONTROL_a_finding_holding_no_zoned_value_is_named_by_its_reproduction():
    zoned = [
        datetime(2026, 1, 1, tzinfo=timezone(timedelta(seconds=30))),
        datetime(2026, 3, 8, 2, 30, tzinfo=ZoneInfo("America/Chicago")),
        time(3, tzinfo=ZoneInfo("America/Chicago")),
    ]
    planted = Finding(SEED, 0, "planted: refused with no zone", "{'n': 1}")
    found = [Finding(SEED, i + 1, "zoned", repr(v)) for i, v in enumerate(zoned)] + [planted]
    with pytest.raises(AssertionError) as caught:
        _assert_each_finding_holds_a_zone_and_each_kind_is_found(found, [*zoned, {"n": 1}])
    assert str(caught.value).startswith(
        "4 finding(s): {'a time whose zone gives no offset': 1, 'a wall time its zone does not "
        "have': 1, 'an offset finer than a minute': 1}; holding no zoned value the read-back "
        "refuses: [\"{'n': 1}\"]"
    ), caught.value


# --- what the skip takes, and what it leaves -------------------------------------------------------


class Item(BaseModel):
    name: str
    qty: int
    price: float


class Holder(BaseModel):
    item: Item
    tags: list[str] = []
    counts: dict[str, int] = {}
    note: str | None = None


def _read_backs(monkeypatch) -> list[type]:
    seen: list[type] = []
    real = validation._reads_as_the_argument

    def counting(cls, wire, value):
        seen.append(cls)
        return real(cls, wire, value)

    monkeypatch.setattr(validation, "_reads_as_the_argument", counting)
    return seen


class Timed(BaseModel):
    at: datetime
    on: date
    when: time
    lasts: timedelta
    naive: datetime | None = None


def test_a_plain_model_holding_temporal_values_is_sent_without_a_read_back(monkeypatch):
    seen = _read_backs(monkeypatch)
    value = Timed(
        at=datetime(2026, 1, 2, 3, 4, tzinfo=timezone(timedelta(hours=5, minutes=30))),
        on=date(2026, 1, 2),
        when=time(3, 4, tzinfo=UTC),
        lasts=timedelta(days=-1, microseconds=7),
        naive=datetime(2026, 1, 2),
    )

    assert wire_models(value) == {
        "at": "2026-01-02T03:04:00+05:30",
        "on": "2026-01-02",
        "when": "03:04:00Z",
        "lasts": "-PT23H59M59.999993S",
        "naive": "2026-01-02T00:00:00",
    }
    assert seen == []


class Priced(BaseModel):
    price: Decimal
    history: list[Decimal] = []
    by_day: dict[str, Decimal] = {}


def test_a_plain_model_holding_decimals_is_sent_without_a_read_back(monkeypatch):
    """Each `Decimal` is written as its text, trailing zeros, exponent, NaN and infinity included."""
    seen = _read_backs(monkeypatch)
    value = Priced.model_construct(
        price=Decimal("1.50"),
        history=[Decimal("-0"), Decimal("1E+5"), Decimal("NaN")],
        by_day={"mon": Decimal("-Infinity")},
    )

    assert wire_models(value) == {
        "price": "1.50",
        "history": ["-0", "1E+5", "NaN"],
        "by_day": {"mon": "-Infinity"},
    }
    assert seen == []


class Shaped(BaseModel):
    point: tuple[int, int]
    tags: tuple[str, ...] = ()
    nothing: tuple[()] = ()
    nested: tuple[tuple[int, ...], ...] = ()
    items: tuple[Item, ...] = ()


def test_a_plain_model_holding_tuples_is_sent_without_a_read_back(monkeypatch):
    seen = _read_backs(monkeypatch)
    value = Shaped(
        point=(1, 2),
        tags=("a", "b"),
        nested=((1,), ()),
        items=(Item(name="a", qty=1, price=1.5),),
    )

    assert wire_models(value) == {
        "point": [1, 2],
        "tags": ["a", "b"],
        "nothing": [],
        "nested": [[1], []],
        "items": [{"name": "a", "qty": 1, "price": 1.5}],
    }
    assert seen == []


def test_a_list_where_a_tuple_is_declared_is_sent_as_before_and_read_as_a_tuple():
    """The walk turns it away, so it takes the read-back, which sends the dump: the service reads
    the same members, as the tuple the field declares."""
    value = Shaped.model_construct(point=[1, 2])

    sent = wire_models(value)

    assert validation._is_plain_instance(value) is False
    assert sent["point"] == [1, 2]
    assert Shaped.model_validate(sent).point == (1, 2)


def test_a_fixed_tuple_of_another_length_is_sent_as_before_and_refused_by_the_service():
    """The walk turns it away, so it takes the read-back, which sends the dump as it is; the
    service refuses it, as it would any form of it."""
    value = Shaped.model_construct(point=(1, 2, 3))

    sent = wire_models(value)

    assert validation._is_plain_instance(value) is False
    assert sent["point"] == [1, 2, 3]
    with pytest.raises(ValidationError):
        Shaped.model_validate(sent)


class Tagged(BaseModel):
    tags: set[str]
    ids: frozenset[int] = frozenset()
    by_day: dict[str, set[int]] = {}


def test_a_plain_model_holding_sets_is_sent_without_a_read_back(monkeypatch):
    seen = _read_backs(monkeypatch)
    value = Tagged(tags={"a", "b", "c"}, ids=frozenset({3, 1, 2}), by_day={"mon": {5}})

    sent = wire_models(value)

    assert seen == []
    assert sorted(sent["tags"]) == ["a", "b", "c"] and len(sent["tags"]) == 3
    assert sorted(sent["ids"]) == [1, 2, 3] and sent["by_day"] == {"mon": [5]}


def test_a_set_is_dumped_in_its_own_order_and_read_as_the_same_set():
    """A set of `str` iterates in an order that follows the process's hash seed, and the dump lists
    the members in that order, so the bytes sent differ between processes. The service reads the
    same set whatever the order, which is all the skip relies on."""
    value = Tagged(tags={"alpha", "beta", "gamma", "delta", "epsilon"})

    sent = wire_models(value)

    assert sent["tags"] == list(value.tags)
    assert Tagged.model_validate(sent) == value


class _Colour(enum.Enum):
    RED = "red"
    BLUE = 2


class _Level(enum.IntEnum):
    LOW = 1


class _Perm(enum.Flag):
    READ = 1
    WRITE = 2


class Kinds(BaseModel):
    colour: _Colour
    level: _Level = _Level.LOW
    perm: _Perm = _Perm.READ
    blob: bytes = b""
    either: int | str = 0
    maybe: float | str | None = None


def test_a_plain_model_holding_enums_bytes_and_unions_is_sent_without_a_read_back(monkeypatch):
    seen = _read_backs(monkeypatch)
    value = Kinds(
        colour=_Colour.BLUE,
        level=_Level.LOW,
        perm=_Perm.READ | _Perm.WRITE,
        blob="\u00e9".encode(),
        either="1",
        maybe=1.5,
    )

    assert wire_models(value) == {
        "colour": 2,
        "level": 1,
        "perm": 3,
        "blob": "\u00e9",
        "either": "1",
        "maybe": 1.5,
    }
    assert seen == []


def test_an_enum_field_holding_a_members_value_is_sent_and_read_as_the_member():
    """Built by `model_construct`, or under `use_enum_values`, an enum field holds the member's
    value. Main sends that value and the service reads the member, so the skip takes it too."""
    value = Kinds.model_construct(colour="red")

    assert validation._is_plain_instance(value) is True
    sent = wire_models(value)
    assert sent["colour"] == "red"
    assert Kinds.model_validate(sent).colour is _Colour.RED


class HoldsAnything(BaseModel):
    anything: Any = None


class TextOrBytes(BaseModel):
    data: str | bytes = ""


class Base64Bytes(BaseModel):
    model_config = ConfigDict(ser_json_bytes="base64")
    blob: bytes = b""


def test_bytes_in_a_union_with_str_are_refused_as_read_back_as_str():
    """`str | bytes` reads the dump of bytes, their text, back as a `str`: another value, which the
    read-back refuses. A skip taking the shape would send it."""
    with pytest.raises(RpcValidationError):
        wire_models(TextOrBytes(data=b"a"))


@pytest.mark.parametrize(
    "cls",
    [
        pytest.param(HoldsAnything, id="an-any-field"),
        pytest.param(TextOrBytes, id="a-union-holding-bytes"),
        pytest.param(Base64Bytes, id="bytes-under-ser-json-bytes"),
    ],
)
def test_a_class_whose_read_back_depends_on_what_it_holds_is_never_plain(monkeypatch, cls):
    """What an `Any` holds, or which member of `str | bytes` a value is, decides how it reads back,
    and main refuses some of each; `ser_json_bytes` writes what the class does not read back. Each
    keeps the read-back."""
    seen = _read_backs(monkeypatch)

    assert validation._plain_check(cls) is None
    wire_models(cls())
    assert seen, "the class was sent without the read-back"


def test_a_plain_model_holding_exactly_its_types_is_sent_without_a_read_back(monkeypatch):
    seen = _read_backs(monkeypatch)
    value = Holder(item=Item(name="a", qty=1, price=1.5), tags=["t"], counts={"k": 1})

    assert wire_models(value) == {
        "item": {"name": "a", "qty": 1, "price": 1.5},
        "tags": ["t"],
        "counts": {"k": 1},
        "note": None,
    }
    assert seen == []


class Normalising(BaseModel):
    name: str

    @field_validator("name")
    @classmethod
    def _strip(cls, value: str) -> str:
        return value.strip()


class InheritsTheValidator(Normalising):
    other: int = 0


class StrictWhen(BaseModel):
    model_config = ConfigDict(strict=True)
    when: datetime


class AliasedInner(BaseModel):
    name: str = Field(alias="Name")


class HoldsAnAliased(BaseModel):
    inner: AliasedInner


class SubItem(Item):
    added: int = 0


def _with(value, **held):
    value.__dict__.update(held)
    return value


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(Normalising(name=" a "), id="a-normalising-validator"),
        pytest.param(InheritsTheValidator(name=" a "), id="a-validator-the-parent-declares"),
        pytest.param(StrictWhen(when=datetime(2026, 1, 1)), id="a-strict-datetime"),
        pytest.param(HoldsAnAliased(inner=AliasedInner(Name="a")), id="an-alias-on-a-nested-field"),
        pytest.param(
            Holder(item=SubItem(name="a", qty=1, price=1.0)), id="a-subclass-in-a-nested-field"
        ),
        pytest.param(
            _with(Item(name="a", qty=1, price=1.0), price=math.nan),
            id="a-nan-assigned-after-construction",
        ),
        pytest.param(Item.model_construct(name="a", qty=1, price=1), id="an-int-where-a-float-is"),
        pytest.param(
            Item.model_construct(name="a", qty=True, price=1.0), id="a-bool-where-an-int-is"
        ),
    ],
)
def test_a_near_miss_still_takes_the_read_back(monkeypatch, value):
    seen = _read_backs(monkeypatch)

    wire_models(value)

    assert seen, "the near miss was sent without the read-back"


_FAR_PAST, _FAR_FUTURE = datetime(2000, 1, 1, tzinfo=UTC), datetime(2999, 1, 1, tzinfo=UTC)


@pytest.mark.parametrize(
    ("annotation", "value"),
    [
        pytest.param(AwareDatetime, _FAR_PAST, id="AwareDatetime"),
        pytest.param(NaiveDatetime, datetime(2026, 1, 2), id="NaiveDatetime"),
        pytest.param(PastDatetime, _FAR_PAST, id="PastDatetime"),
        pytest.param(FutureDatetime, _FAR_FUTURE, id="FutureDatetime"),
        pytest.param(PastDate, date(2000, 1, 1), id="PastDate"),
        pytest.param(FutureDate, date(2999, 1, 1), id="FutureDate"),
    ],
)
def test_a_constrained_temporal_type_is_not_its_base_type_and_takes_the_read_back(
    monkeypatch, annotation, value
):
    """Each validates more than its base type does, so its class is not plain."""
    cls = create_model(f"Holds{annotation.__name__}", v=(annotation, ...))
    seen = _read_backs(monkeypatch)

    wire_models(cls(v=value))

    assert validation._plain_check(cls) is None
    assert seen, "the constrained type was sent without the read-back"


# --- the instance walk ----------------------------------------------------------------------------


class _Instant(datetime):
    pass


class _Span(timedelta):
    pass


class _Amount(Decimal):
    pass


class _Pair(tuple):
    pass


class _Bag(frozenset):
    pass


def _timed(**held):
    """A plain `Timed` with whole-minute zones, then `held` set by `model_construct`."""
    values = {
        "at": datetime(2026, 1, 2, tzinfo=UTC),
        "on": date(2026, 1, 2),
        "when": time(3, tzinfo=timezone(timedelta(hours=-5))),
        "lasts": timedelta(seconds=1),
        "naive": None,
    }
    return Timed.model_construct(**{**values, **held})


def _assigned(value, **held):
    value.__dict__.update(held)
    return value


@pytest.mark.parametrize(
    ("value", "plain"),
    [
        pytest.param(Item(name="a", qty=1, price=1.5), True, id="plain"),
        pytest.param(Holder(item=SubItem(name="a", qty=1, price=1.0)), False, id="sub-in-base"),
        pytest.param(_assigned(Item(name="a", qty=1, price=1.0), price=math.nan), False, id="nan"),
        pytest.param(_assigned(Item(name="a", qty=1, price=1.0), price=math.inf), False, id="inf"),
        pytest.param(Item.model_construct(name="a", qty=True, price=1.0), False, id="bool-for-int"),
        pytest.param(Item.model_construct(name="a", qty=1), False, id="a-missing-field"),
        pytest.param(
            Item.model_construct(name="\ud800", qty=1, price=1.0), False, id="a-lone-surrogate"
        ),
        pytest.param(_timed(), True, id="plain-temporal"),
        pytest.param(
            _timed(at=datetime(2026, 1, 2, tzinfo=timezone(timedelta(seconds=30)))),
            False,
            id="a-datetime-offset-finer-than-a-minute",
        ),
        pytest.param(
            _timed(when=time(3, tzinfo=timezone(timedelta(microseconds=5)))),
            False,
            id="a-time-offset-finer-than-a-minute",
        ),
        pytest.param(
            _timed(at=datetime(2026, 7, 1, tzinfo=ZoneInfo("America/Chicago"))),
            False,
            id="a-zoneinfo-datetime",
        ),
        pytest.param(_timed(on=datetime(2026, 1, 2)), False, id="a-datetime-for-a-date"),
        pytest.param(_timed(at=_Instant(2026, 1, 2)), False, id="a-datetime-subclass"),
        pytest.param(_timed(lasts=_Span(days=1)), False, id="a-timedelta-subclass"),
        pytest.param(Shaped(point=(1, 2), tags=("a",)), True, id="plain-tuples"),
        pytest.param(Shaped.model_construct(point=[1, 2]), False, id="a-list-where-a-tuple-is"),
        pytest.param(Shaped.model_construct(point=_Pair((1, 2))), False, id="a-tuple-subclass"),
        pytest.param(
            Shaped.model_construct(point=(1, 2, 3)), False, id="a-fixed-tuple-of-another-length"
        ),
        pytest.param(Shaped.model_construct(point=(True, 2)), False, id="a-bool-in-a-fixed-tuple"),
        pytest.param(
            Shaped.model_construct(point=(1, 2), tags=["a"]),
            False,
            id="a-list-where-a-variadic-tuple-is",
        ),
        pytest.param(
            Shaped.model_construct(point=(1, 2), tags=("a", 1)),
            False,
            id="an-int-in-a-tuple-of-str",
        ),
        pytest.param(
            Shaped.model_construct(point=(1, 2), nothing=(1,)),
            False,
            id="a-member-in-an-empty-tuple",
        ),
        pytest.param(Kinds(colour=_Colour.RED), True, id="plain-kinds"),
        pytest.param(Kinds.model_construct(colour="red"), True, id="an-enum-members-value"),
        pytest.param(Kinds.model_construct(colour="green"), False, id="a-value-no-member-holds"),
        pytest.param(Kinds.model_construct(colour=_Level.LOW), False, id="another-enums-member"),
        pytest.param(
            Kinds.model_construct(colour=_Colour.RED, level=True), False, id="true-for-an-intenum"
        ),
        pytest.param(
            Kinds.model_construct(colour=_Colour.RED, blob=b"\xff"), False, id="bytes-not-utf8"
        ),
        pytest.param(
            Kinds.model_construct(colour=_Colour.RED, blob=bytearray(b"a")), False, id="a-bytearray"
        ),
        pytest.param(
            Kinds.model_construct(colour=_Colour.RED, either=True),
            False,
            id="a-bool-in-a-union-of-int-and-str",
        ),
        pytest.param(
            Kinds.model_construct(colour=_Colour.RED, either=None),
            False,
            id="none-in-a-union-without-none",
        ),
        pytest.param(
            Kinds.model_construct(colour=_Colour.RED, maybe=None),
            True,
            id="none-in-an-optional-union",
        ),
        pytest.param(Tagged(tags={"a"}, ids=frozenset({1})), True, id="plain-sets"),
        pytest.param(Tagged.model_construct(tags=["a"]), False, id="a-list-where-a-set-is"),
        pytest.param(
            Tagged.model_construct(tags=frozenset({"a"})), False, id="a-frozenset-where-a-set-is"
        ),
        pytest.param(
            Tagged.model_construct(tags={"a"}, ids={1}), False, id="a-set-where-a-frozenset-is"
        ),
        pytest.param(
            Tagged.model_construct(tags={"a"}, ids=_Bag({1})), False, id="a-frozenset-subclass"
        ),
        pytest.param(Tagged.model_construct(tags={"a", 1}), False, id="an-int-in-a-set-of-str"),
        pytest.param(
            Tagged.model_construct(tags={"a"}, by_day={"mon": {True}}),
            False,
            id="a-bool-in-a-nested-set-of-int",
        ),
        pytest.param(Priced.model_construct(price=Decimal("NaN")), True, id="a-decimal-nan"),
        pytest.param(Priced.model_construct(price=_Amount("1.5")), False, id="a-decimal-subclass"),
        pytest.param(Priced.model_construct(price=1), False, id="an-int-for-a-decimal"),
        pytest.param(Priced.model_construct(price="1.5"), False, id="a-str-for-a-decimal"),
        pytest.param(
            Priced.model_construct(price=Decimal(1), history=[1.5]),
            False,
            id="a-float-in-a-list-of-decimals",
        ),
    ],
)
def test_the_walk_takes_only_an_instance_that_holds_exactly_its_types(value, plain):
    assert validation._plain_check(type(value)) is not None, "the class itself is not plain"
    assert validation._is_plain_instance(value) is plain


# --- the class test's defence branches --------------------------------------------------------------


class WithAComputedField(BaseModel):
    n: int

    @computed_field  # type: ignore[prop-decorator]
    @property
    def doubled(self) -> int:
        return self.n * 2


class WithConstraintMetadata(BaseModel):
    """`annotated_types.Gt` sets no field option, so only its metadata says the field validates."""

    n: Annotated[int, annotated_types.Gt(0)]


class ARootModel(RootModel[int]):
    pass


class WithItsOwnEq(BaseModel):
    n: int

    def __eq__(self, other: object) -> bool:
        return super().__eq__(other)

    __hash__ = None  # type: ignore[assignment]


class WithItsOwnPostInit(BaseModel):
    n: int

    def model_post_init(self, context: object, /) -> None:
        pass


class WithItsOwnSchemaHook(BaseModel):
    n: int

    @classmethod
    def __get_pydantic_core_schema__(cls, source, handler):
        return handler(source)


class OptionalOfANonPlainType(BaseModel):
    number: complex | None = None


@pytest.mark.parametrize(
    "cls",
    [
        pytest.param(WithAComputedField, id="a-computed-field"),
        pytest.param(WithConstraintMetadata, id="annotated-metadata"),
        pytest.param(ARootModel, id="a-root-model"),
        pytest.param(WithItsOwnEq, id="its-own-eq"),
        pytest.param(WithItsOwnPostInit, id="its-own-model-post-init"),
        pytest.param(WithItsOwnSchemaHook, id="its-own-schema-hook"),
        pytest.param(OptionalOfANonPlainType, id="optional-of-a-non-plain-type"),
    ],
)
def test_a_class_one_defence_away_from_plain_is_not_plain(cls):
    assert validation._plain_check(cls) is None


def test_an_instance_holding_extras_is_not_taken_as_plain():
    value = Item(name="a", qty=1, price=1.0)
    object.__setattr__(value, "__pydantic_extra__", {"stray": 1})

    assert validation._plain_check(Item) is not None, "the class itself is not plain"
    assert validation._is_plain_instance(value) is False


def test_a_field_whose_options_cannot_be_read_makes_its_class_not_plain_and_still_sends(
    monkeypatch,
):
    """`FieldInfo._attributes_set` is pydantic's, not public API: without it no class is plain,
    and a model is sent through the read-back as before instead of every send raising."""

    class Fresh(BaseModel):
        n: int

    _fresh_cache(monkeypatch)
    monkeypatch.delattr(FieldInfo, "_attributes_set")

    assert validation._plain_check(Fresh) is None
    assert wire_models(Fresh(n=1)) == {"n": 1}
