"""Models for the plain-model check: plain ones, near misses, and instances that hold the wrong type.

`generate(rng, index)` builds one model class (by `create_model`) and an instance of it, described
in text for a failure to print. A class is either plain (fields of `str`, `int`, `bool`, `float`,
`Decimal`, `bytes`, an `Enum`, a union of two scalar types, `datetime`, `date`, `time`, `timedelta`,
`X | None`, `list[X]`, `tuple[X, ...]`, a fixed tuple, `set[X]`, `frozenset[X]`, `dict[str, X]` and
plain nested models, configs that are inert) or a near miss that differs from plain in one named
way: a config key, a field option, a validator (its own or inherited), a serializer, a computed
field, or `extra`. An instance is built by the constructor, or by `model_construct` and direct
assignment with a value its field's type does not hold: an int past 64 bits, a lone surrogate, -0.0,
NaN, infinity, an empty list or dict, a dict key of a `str` subclass, a bool where an int is
declared, an int where a float is, a datetime where a date is, a subclass of a temporal type or of
`Decimal`, an ISO string where a temporal type is, an int, a float or a str where a `Decimal` is, a
`Decimal` NaN or infinity (which validation refuses), a list where a tuple is declared, a tuple
subclass, a tuple of another length, a list or the other kind of set where a set is declared, a
frozenset subclass, bytes that are not UTF-8, a bytearray or a str for bytes, an enum member's value
or another enum's member or `True` where an enum is declared, a value of neither type or bytes or
`None` in a union of two scalars, a subclass instance in a nested field, a nested instance shared by
two fields, or a field left out. A temporal value is drawn from its edges as well: naive and aware,
a fixed offset (whole minutes, finer than a minute, and the extremes), a `ZoneInfo` outside and
inside a fold or a gap, `fold=1`, the minimum and maximum, and negative, tiny and huge durations.
"""

from __future__ import annotations

import enum
import random
import typing
from datetime import UTC, date, datetime, time, timedelta, timezone
from decimal import Decimal
from typing import Any
from zoneinfo import ZoneInfo

from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    computed_field,
    create_model,
    field_serializer,
    field_validator,
    model_validator,
)


class Text(str):
    """A `str` subclass: a dict key or a value of it is not exactly a `str`."""


class Instant(datetime):
    """A `datetime` subclass: a value of it is not exactly a `datetime`."""


class Day(date):
    """A `date` subclass."""


class Clock(time):
    """A `time` subclass."""


class Span(timedelta):
    """A `timedelta` subclass."""


class Amount(Decimal):
    """A `Decimal` subclass."""


class Pair(tuple):
    """A `tuple` subclass."""


class Bag(frozenset):
    """A `frozenset` subclass."""


#: Decimals the constructor accepts: trailing zeros and exponents are kept, so each is written as
#: held, and digits past the context's precision are kept too.
DECIMALS: list[Decimal] = [
    Decimal("1"),
    Decimal("1.00"),
    Decimal("-0"),
    Decimal("0E-10"),
    Decimal("1E+5"),
    Decimal("12345678901234567890123456789012345678901234567890"),
    Decimal("9.999999999999999999999999999E+999999"),
    Decimal("1E-999999"),
]


_CHICAGO = ZoneInfo("America/Chicago")
_ZONES = [
    None,
    UTC,
    timezone(timedelta(hours=5, minutes=30)),
    timezone(timedelta(hours=-23, minutes=-59)),
    timezone(timedelta(hours=1), "CET"),
    timezone(timedelta(seconds=30)),
    timezone(timedelta(microseconds=5)),
]
#: Datetimes at the edges: each zone above, and a `ZoneInfo` outside a transition, in the ambiguous
#: hour (both folds) and in the skipped hour.
DATETIMES: list[datetime] = [
    *(datetime(2026, 1, 2, 3, 4, 5, 6, tzinfo=zone) for zone in _ZONES),
    datetime(2026, 7, 1, 12, tzinfo=_CHICAGO),
    datetime(2026, 11, 1, 1, 30, tzinfo=_CHICAGO),
    datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=_CHICAGO),
    datetime(2026, 3, 8, 2, 30, tzinfo=_CHICAGO),
    datetime(2026, 11, 1, 1, 30, fold=1),
    datetime(2026, 11, 1, 1, 30, fold=1, tzinfo=timezone(timedelta(hours=-5))),
    datetime.min,
    datetime.max,
    datetime(1, 1, 1, tzinfo=UTC),
    datetime(9999, 12, 31, 23, 59, 59, 999999, tzinfo=UTC),
]
DATES: list[date] = [date(2026, 1, 2), date.min, date.max, date(1970, 1, 1)]
TIMES: list[time] = [
    *(time(3, 4, 5, 6, tzinfo=zone) for zone in _ZONES),
    time(3, tzinfo=_CHICAGO),
    time(1, 30, fold=1),
    time.min,
    time.max,
]
TIMEDELTAS: list[timedelta] = [
    timedelta(0),
    timedelta(days=1, seconds=2, microseconds=3),
    timedelta(microseconds=1),
    timedelta(microseconds=-1),
    timedelta(days=-1),
    timedelta(hours=-25, microseconds=7),
    timedelta.min,
    timedelta.max,
]
TEMPORALS: dict[type, list[Any]] = {
    datetime: DATETIMES,
    date: DATES,
    time: TIMES,
    timedelta: TIMEDELTAS,
}


#: Configs a plain class may hold: inert keys at any value, and keys at pydantic's default.
INERT_CONFIGS: list[dict[str, Any]] = [
    {"use_enum_values": True},
    {},
    {"frozen": True},
    {"title": "Titled"},
    {"validate_assignment": True},
    {"hide_input_in_errors": True},
    {"extra": "ignore"},
    {"strict": False},
]

#: One config key each, set so that a class holding it is not plain.
NEAR_MISS_CONFIGS: list[dict[str, Any]] = [
    {"str_strip_whitespace": True},
    {"str_to_lower": True},
    {"str_to_upper": True},
    {"str_max_length": 3},
    {"coerce_numbers_to_str": True},
    {"strict": True},
    {"ser_json_inf_nan": "constants"},
    {"ser_json_bytes": "base64"},
    {"ser_json_timedelta": "float"},
    {"serialize_by_alias": True},
    {"alias_generator": str.upper, "populate_by_name": True},
    {"alias_generator": str.upper, "validate_by_alias": False, "validate_by_name": True},
    {"extra": "allow"},
    {"extra": "forbid"},
]

#: One field option each that makes a class not plain.
NEAR_MISS_FIELDS: list[tuple[str, dict[str, Any]]] = [
    ("alias", {"alias": "Aliased"}),
    ("validation_alias", {"validation_alias": "read_from"}),
    ("serialization_alias", {"serialization_alias": "written_as"}),
    ("exclude", {"exclude": True}),
    ("max_length", {"max_length": 3}),
    ("pattern", {"pattern": "^a"}),
    ("ge", {"ge": 0}),
    ("strict", {"strict": True}),
    ("json_schema_extra", {"json_schema_extra": {"x": 1}}),
]


class Colour(enum.Enum):
    RED = "red"
    BLUE = 2


class Level(enum.IntEnum):
    LOW = 1
    HIGH = 2


class Shade(enum.StrEnum):
    DARK = "dark"


class Perm(enum.Flag):
    READ = 1
    WRITE = 2


ENUMS: list[type[enum.Enum]] = [Colour, Level, Shade, Perm]
SCALARS: list[type] = [str, int, bool, float, Decimal, bytes, *ENUMS, *TEMPORALS]
#: Unions of two scalar types the plain grammar takes, each read back as the type the value holds.
UNIONS: list[Any] = [int | str, str | int, int | float, bool | int, int | bool, float | str]


def _scalar_value(rng: random.Random, kind: type) -> Any:
    if kind is str:
        return rng.choice(["a", " padded ", "MiXeD", ""])
    if kind is int:
        return rng.choice([0, 1, -7, 2**40])
    if kind is bool:
        return rng.choice([True, False])
    if kind in TEMPORALS:
        return rng.choice(TEMPORALS[kind])
    if kind is Decimal:
        return rng.choice(DECIMALS)
    if kind is bytes:
        return rng.choice([b"", b"abc", "\u00e9".encode()])
    if kind in ENUMS:
        members = list(kind)
        if kind is Perm:
            members.append(Perm.READ | Perm.WRITE)
        return rng.choice(members)
    return rng.choice([0.0, 1.5, -2.25])


def _wrong_value(rng: random.Random, kind: type) -> tuple[Any, str]:
    """A value that `kind`'s field accepts on validation but does not hold as exactly `kind`, or one
    it holds as exactly `kind` that a JSON dump or a read changes: what the walk must catch."""
    if kind is str:
        return rng.choice([(Text("t"), "a str subclass"), ("\ud800", "a lone surrogate")])
    if kind is int:
        return rng.choice([(True, "a bool for an int"), (2**70, "an int past 64 bits")])
    if kind is bool:
        return (1, "an int for a bool")
    if kind is bytes:
        return rng.choice(
            [
                (b"\xff\x00", "bytes that are not UTF-8"),
                (bytearray(b"a"), "a bytearray for bytes"),
                ("a", "a str for bytes"),
            ]
        )
    if kind in ENUMS:
        member = next(iter(kind))
        other = Colour.RED if kind is not Colour else Level.LOW
        return rng.choice(
            [
                (member.value, "a member's value held where the member is declared"),
                (other, "a member of another enum"),
                (True if kind is Level else member.value, "True for an IntEnum member"),
            ]
        )
    if kind is Decimal:
        return rng.choice(
            [
                (Amount("1.5"), "a Decimal subclass"),
                (1, "an int for a Decimal"),
                (1.5, "a float for a Decimal"),
                ("1.5", "a str for a Decimal"),
                (Decimal("NaN"), "a Decimal NaN"),
                (Decimal("sNaN"), "a signalling Decimal NaN"),
                (Decimal("Infinity"), "a Decimal infinity"),
                (Decimal("-Infinity"), "a negative Decimal infinity"),
            ]
        )
    if kind is datetime:
        return rng.choice(
            [
                (Instant(2026, 1, 2), "a datetime subclass"),
                ("2026-01-02T03:04:05", "an ISO string for a datetime"),
            ]
        )
    if kind is date:
        return rng.choice(
            [
                (datetime(2026, 1, 2), "a datetime for a date"),
                (Day(2026, 1, 2), "a date subclass"),
                ("2026-01-02", "an ISO string for a date"),
            ]
        )
    if kind is time:
        return rng.choice(
            [(Clock(3, 4), "a time subclass"), ("03:04:05", "an ISO string for a time")]
        )
    if kind is timedelta:
        return rng.choice(
            [(Span(days=1), "a timedelta subclass"), (86400, "an int for a timedelta")]
        )
    return rng.choice(
        [
            (1, "an int for a float"),
            (float("nan"), "NaN"),
            (float("inf"), "infinity"),
            (-0.0, "-0.0"),
        ]
    )


def _annotation(rng: random.Random, depth: int, nested: list[type]) -> tuple[Any, str, Any]:
    """An annotation in the plain grammar, its description, and a valid value for it."""
    r = rng.random()
    if r < 0.45 or depth >= 2:
        kind = rng.choice(SCALARS)
        return kind, kind.__name__, _scalar_value(rng, kind)
    if r < 0.6:
        inner, said, value = _annotation(rng, depth + 1, nested)
        return inner | None, f"{said} | None", rng.choice([value, None])
    if r < 0.75:
        inner, said, value = _annotation(rng, depth + 1, nested)
        return list[inner], f"list[{said}]", rng.choice([[value], [value, value], []])
    if r < 0.8:
        inner, said, value = _annotation(rng, depth + 1, nested)
        return dict[str, inner], f"dict[str, {said}]", rng.choice([{"k": value}, {}])
    if r < 0.79:
        union = rng.choice(UNIONS)
        kind = rng.choice(typing.get_args(union))
        return (
            union,
            " | ".join(t.__name__ for t in typing.get_args(union)),
            _scalar_value(rng, kind),
        )
    if r < 0.82:
        kind = rng.choice(SCALARS)
        members = {_scalar_value(rng, kind) for _ in range(rng.randint(0, 3))}
        container = rng.choice([set, frozenset])
        return container[kind], f"{container.__name__}[{kind.__name__}]", container(members)
    if r < 0.85:
        if rng.random() < 0.5:
            inner, said, value = _annotation(rng, depth + 1, nested)
            return (
                tuple[inner, ...],
                f"tuple[{said}, ...]",
                rng.choice([(value,), (value, value), ()]),
            )
        first, said_first, value_first = _annotation(rng, depth + 1, nested)
        second, said_second, value_second = _annotation(rng, depth + 1, nested)
        return (
            tuple[first, second],
            f"tuple[{said_first}, {said_second}]",
            (value_first, value_second),
        )
    cls, said, value = _plain_class(rng, depth + 1, nested)
    return cls, said, value


def _plain_class(rng: random.Random, depth: int, nested: list[type]) -> tuple[type, str, Any]:
    fields: dict[str, Any] = {}
    values: dict[str, Any] = {}
    described: list[str] = []
    for k in range(rng.randint(1, 3)):
        annotation, said, value = _annotation(rng, depth, nested)
        fields[f"f{k}"] = (annotation, ...)
        values[f"f{k}"] = value
        described.append(f"f{k}: {said}")
    config = rng.choice(INERT_CONFIGS)
    cls = create_model(f"Plain{depth}_{len(nested)}", __config__=ConfigDict(**config), **fields)
    nested.append(cls)
    return cls, f"{cls.__name__}({', '.join(described)}; config {config})", cls(**values)


def _near_miss_config_class(config: dict[str, Any]) -> tuple[type, str, dict[str, Any]]:
    cls = create_model("NearConfig", __config__=ConfigDict(**config), name=(str, ...))
    return cls, f"config {config}", {"name": " A ", "Name": " A ", "NAME": " A "}


def _build(cls: type, said: str, raw: dict[str, Any]) -> tuple[Any, str]:
    names = set(cls.model_fields) | {f.alias for f in cls.model_fields.values() if f.alias}
    values = {k: v for k, v in raw.items() if k in names}
    try:
        return cls.model_validate(values), f"near miss: {said}"
    except Exception:
        return cls.model_construct(**values), f"near miss: {said} (model_construct)"


def near_miss_with_config(config: dict[str, Any]) -> tuple[Any, str]:
    """The near miss `generate` draws for `config`, built the same way, for a check that must see
    every config key rather than the ones a seed happens to draw."""
    return _build(*_near_miss_config_class(config))


def _near_miss(rng: random.Random) -> tuple[type, str, dict[str, Any]]:
    """A class one named step from plain, and the values to build it with."""
    choice = rng.randrange(6)
    if choice == 0:
        return _near_miss_config_class(rng.choice(NEAR_MISS_CONFIGS))
    if choice == 1:
        said, options = rng.choice(NEAR_MISS_FIELDS)
        cls = create_model("NearField", name=(str, Field(**options)))
        return (
            cls,
            f"field option {said}",
            {
                "name": "abc",
                "Aliased": "abc",
                "read_from": "abc",
            },
        )
    if choice == 2:

        class Normalising(BaseModel):
            name: str

            @field_validator("name")
            @classmethod
            def _strip(cls, value: str) -> str:
                return value.strip()

        cls = create_model("InheritsAValidator", __base__=Normalising, other=(int, 0))
        return cls, "a subclass whose parent declares a normalising validator", {"name": " a "}
    if choice == 3:

        class After(BaseModel):
            n: int

            @model_validator(mode="after")
            def _same(self) -> After:
                return self

        return After, "a model validator", {"n": 1}
    if choice == 4:

        class Serialized(BaseModel):
            n: int

            @field_serializer("n")
            def _twice(self, value: int) -> int:
                return value * 2

        return Serialized, "a field serializer", {"n": 2}

    class Computed(BaseModel):
        n: int

        @computed_field  # type: ignore[prop-decorator]
        @property
        def doubled(self) -> int:
            return self.n * 2

    return Computed, "a computed field", {"n": 3}


def generate(rng: random.Random, index: int) -> tuple[Any, str]:
    """One instance to publish, and its description."""
    if rng.random() < 0.3:
        return _build(*_near_miss(rng))

    nested: list[type] = []
    cls, said, value = _plain_class(rng, 0, nested)
    how = rng.random()
    if how < 0.4:
        return value, f"plain {said}, constructed"
    name = rng.choice(list(cls.model_fields))
    annotation = cls.model_fields[name].annotation
    held = dict(value.__dict__)
    if how < 0.55:
        held.pop(name)
        return cls.model_construct(**held), f"plain {said}, {name} left out by model_construct"
    if annotation in SCALARS:
        wrong, why = _wrong_value(rng, annotation)
    elif isinstance(annotation, type) and issubclass(annotation, BaseModel):
        sub = create_model(f"Sub{annotation.__name__}", __base__=annotation, added=(int, 0))
        current = held[name]
        if rng.random() < 0.5:
            wrong, why = sub(**current.__dict__, added=5), "a subclass instance in a nested field"
        else:
            other = rng.choice([n for n in cls.model_fields if n != name] or [name])
            held[other] = (
                current if cls.model_fields[other].annotation is annotation else held[other]
            )
            wrong, why = current, "one nested instance shared by two fields"
    elif annotation is not None and getattr(annotation, "__origin__", None) is dict:
        wrong, why = (
            {Text("k"): v for v in list(held[name].values())[:1]} or {Text("k"): None},
            ("a dict key of a str subclass"),
        )
    elif getattr(annotation, "__origin__", None) is list:
        wrong, why = [], "an empty list"
    elif annotation in UNIONS:
        allowed = typing.get_args(annotation)
        wrong, why = rng.choice(
            [
                (
                    True if bool not in allowed else "s" if str not in allowed else 1.5,
                    "a value of neither type",
                ),
                (b"a", "bytes in a union of two scalars"),
                (None, "None in a union without None"),
            ]
        )
    elif getattr(annotation, "__origin__", None) in (set, frozenset):
        current, origin = held[name], annotation.__origin__
        other = frozenset if origin is set else set
        wrong, why = rng.choice(
            [
                (list(current), f"a list where a {origin.__name__} is declared"),
                (other(current), f"a {other.__name__} where a {origin.__name__} is declared"),
                (Bag(current), "a frozenset subclass"),
            ]
        )
    elif getattr(annotation, "__origin__", None) is tuple:
        current = held[name]
        wrong, why = rng.choice(
            [
                (list(current), "a list where a tuple is declared"),
                (Pair(current), "a tuple subclass"),
                ((*current, current[0]) if current else (None,), "a tuple of another length"),
            ]
        )
    else:
        wrong, why = None, "None"
    if how < 0.8:
        held[name] = wrong
        return cls.model_construct(**held), f"plain {said}, {name} = {why} by model_construct"
    value.__dict__[name] = wrong
    return value, f"plain {said}, {name} = {why} assigned after construction"
