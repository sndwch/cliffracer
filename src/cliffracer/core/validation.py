"""Runtime validation helpers for timeouts, batch sizes, strings, and payload encoding."""

import dataclasses
import datetime as dt
import decimal
import enum
import json
import math
import threading
import types
import typing
import weakref
from collections.abc import Callable, Iterable, Mapping
from typing import Any, Literal, cast

try:
    import msgpack
except ImportError:
    msgpack = None
import pydantic_core
from pydantic import AliasChoices, AliasPath, BaseModel, RootModel
from pydantic import ValidationError as PydanticValidationError
from pydantic.fields import FieldInfo

from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.exceptions import ValidationError as CliffracerValidationError

from .error_text import own_text


class ValidationError(CliffracerValidationError, ValueError):
    """Raised when one of the framework's argument checks fails.

    The same class as `cliffracer.ValidationError` for an `except` clause, and still a
    `ValueError`, so a caller that caught that keeps catching this. A pydantic model or a
    `ServiceConfig` that fails validation raises pydantic's own `ValidationError`, which is a
    different class.
    """


class NumericBounds:
    """Common numeric validation bounds"""

    # Timeouts (milliseconds)
    MIN_TIMEOUT_MS = 1
    MAX_TIMEOUT_MS = 3600000  # 1 hour

    # Batch sizes
    MIN_BATCH_SIZE = 1
    MAX_BATCH_SIZE = 10000

    # Concurrent operations
    MAX_CONCURRENT = 1000


class StringLimits:
    """Common string length limits"""

    # User input
    MIN_USERNAME_LENGTH = 3
    MAX_USERNAME_LENGTH = 32

    MIN_PASSWORD_LENGTH = 8
    MAX_PASSWORD_LENGTH = 128


def validate_timeout(
    timeout: int | float, min_ms: int | None = None, max_ms: int | None = None
) -> float:
    """
    Validate a timeout expressed in SECONDS.

    The unit is the parameter's, not the value's. Inferring it from the
    magnitude -- `timeout * 1000 if timeout < 1000 else timeout` -- made every
    value of 1000 or more come back a thousand times smaller with no error and
    no log line, so `NumericBounds.MAX_TIMEOUT_MS` (an hour) could not be
    reached from this side at all: `validate_timeout(3600)` returned 3.6.

    The bounds stay in milliseconds because `NumericBounds` states them that
    way and `MIN_TIMEOUT_MS = 1` has no whole-second equivalent. So the units
    are mixed by design: seconds in, milliseconds for the bounds, seconds out,
    and every message below names which.

    Args:
        timeout: Timeout in seconds. Fractions are allowed.
        min_ms: Minimum permitted timeout, in MILLISECONDS.
        max_ms: Maximum permitted timeout, in MILLISECONDS.

    Returns:
        The validated timeout, in seconds -- the value that was passed in.

    Raises:
        ValidationError: If the timeout is not a finite number, or falls
            outside the permitted bounds.
    """
    # `bool` before `int`, because `isinstance(True, int)` is True and a flag is
    # not a duration: `validate_timeout(True)` used to return 1.0 second.
    if isinstance(timeout, bool) or not isinstance(timeout, int | float):
        raise ValidationError(f"Timeout must be numeric, got {type(timeout).__name__}")

    # nan compares False against both bounds, so it passed the range check
    # untouched and was returned as nan. A nan reaching `asyncio.wait_for` is a
    # hang, not a timeout. inf fails the range check, but says so in ms.
    if not math.isfinite(timeout):
        raise ValidationError(f"Timeout must be a finite number of seconds, got {timeout}")

    timeout_ms = timeout * 1000

    # `is None`, not `or`: an explicit 0 (no lower bound) is a bound, not a request for the default.
    min_ms = NumericBounds.MIN_TIMEOUT_MS if min_ms is None else min_ms
    max_ms = NumericBounds.MAX_TIMEOUT_MS if max_ms is None else max_ms

    if timeout_ms < min_ms or timeout_ms > max_ms:
        raise ValidationError(
            f"Timeout must be between {min_ms}ms and {max_ms}ms, got "
            f"{timeout} seconds ({timeout_ms}ms)"
        )

    # The value that came in, not a round trip through milliseconds.
    return float(timeout)


def validate_batch_size(batch_size: int) -> int:
    """
    Validate batch size.

    Args:
        batch_size: Number of items per batch

    Returns:
        Validated batch size

    Raises:
        ValidationError: If batch size is invalid
    """
    # `bool` first: `isinstance(True, int)` is True, and a flag passed where a
    # count belongs would otherwise be accepted as a batch of one.
    if isinstance(batch_size, bool):
        raise ValidationError("Batch size must be an integer, got bool")
    if not isinstance(batch_size, int):
        raise ValidationError(f"Batch size must be an integer, got {type(batch_size).__name__}")

    if batch_size < NumericBounds.MIN_BATCH_SIZE or batch_size > NumericBounds.MAX_BATCH_SIZE:
        raise ValidationError(
            f"Batch size must be between {NumericBounds.MIN_BATCH_SIZE} and "
            f"{NumericBounds.MAX_BATCH_SIZE}, got {batch_size}"
        )

    return batch_size


def validate_string_length(
    value: str,
    min_length: int | None = None,
    max_length: int | None = None,
    field_name: str = "String",
) -> str:
    """
    Validate string length.

    Args:
        value: String to validate
        min_length: Minimum allowed length
        max_length: Maximum allowed length
        field_name: Name of field for error messages

    Returns:
        Validated string

    Raises:
        ValidationError: If string is invalid
    """
    if not isinstance(value, str):
        raise ValidationError(f"{field_name} must be a string, got {type(value).__name__}")

    length = len(value)

    if min_length is not None and length < min_length:
        raise ValidationError(
            f"{field_name} must be at least {min_length} characters, got {length}"
        )

    if max_length is not None and length > max_length:
        raise ValidationError(f"{field_name} must be at most {max_length} characters, got {length}")

    return value


def normalize_username(username: str) -> str:
    """The form a username is stored and looked up under: lowercased.

    One function, so every place that stores a user and every place that looks
    one up agree on the key.
    """
    return username.lower()


def validate_username(username: str) -> str:
    """
    Validate a username and return its normalised form.

    Letters and numbers are any Unicode letters and numbers, not only ASCII, and the
    name is lowercased and not otherwise normalised: two names that render alike in
    different scripts (a Latin and a Cyrillic "a") are different usernames.

    Args:
        username: Username to validate

    Returns:
        The username lowercased by `normalize_username`, which is the form a
        caller must store and look it up under

    Raises:
        ValidationError: If username is invalid
    """
    username = validate_string_length(
        username,
        min_length=StringLimits.MIN_USERNAME_LENGTH,
        max_length=StringLimits.MAX_USERNAME_LENGTH,
        field_name="Username",
    )

    # Additional username validation
    if not username.replace("_", "").replace("-", "").replace(".", "").isalnum():
        raise ValidationError(
            "Username can only contain letters, numbers, underscores, hyphens, and dots"
        )

    return normalize_username(username)


def validate_password(password: str) -> str:
    """
    Validate password.

    Two things are checked: the length, from `StringLimits.MIN_PASSWORD_LENGTH` to
    `MAX_PASSWORD_LENGTH` characters, and that the password is not made of whitespace alone
    (`str.isspace`: spaces, tabs, newlines, and the Unicode spaces such as U+00A0 and U+3000).
    There are no composition rules, as NIST SP 800-63B advises, and nothing is checked against
    known-breached passwords. A character that is invisible but not whitespace, such as the
    zero-width space U+200B, is not whitespace and passes.

    Args:
        password: Password to validate

    Returns:
        The password, unchanged: surrounding whitespace is kept

    Raises:
        ValidationError: If the password is not a string, is shorter or longer than the limits,
            or is whitespace alone
    """
    password = validate_string_length(
        password,
        min_length=StringLimits.MIN_PASSWORD_LENGTH,
        max_length=StringLimits.MAX_PASSWORD_LENGTH,
        field_name="Password",
    )
    if password.isspace():
        raise ValidationError("Password must contain at least one character that is not whitespace")
    return password


# Serialization formats and Content-Type constants
CONTENT_TYPE_JSON = "application/json"
CONTENT_TYPE_MSGPACK = "application/msgpack"


_packers = threading.local()


def _packer() -> Any:
    """This thread's `msgpack.Packer`, built on first use.

    `msgpack.packb` builds a packer for every call. A packer keeps an internal buffer, so one
    shared by two threads would interleave their output; one per thread is safe because a pack is
    synchronous, so a thread finishes one before it starts the next (it is reset when a pack
    raises). The packer is built with the options `packb` defaults to, so the bytes are the same.
    """
    try:
        return _packers.packer
    except AttributeError:
        packer = _packers.packer = msgpack.Packer(use_bin_type=True)
        return packer


def pack_msgpack(data: Any) -> bytes:
    """Pack data into msgpack bytes, with the same values JSON would carry.

    The whole payload goes through `pydantic_core.to_jsonable_python` first, as
    it does for JSON, so the format never changes what a receiver decodes:
    integer map keys become strings, bytes become `str`, and bytes that are not
    UTF-8 raise here, in the sender. Used only as a fallback for values msgpack
    could not encode, it let integer keys through to a receiver that refuses
    them and delivered bytes where JSON delivered strings.
    """
    if msgpack is None:
        raise own_text(
            ImportError(
                "MessagePack serialization requires the 'msgpack' package. "
                "Install it with: pip install 'cliffracer[msgpack]'"
            )
        )
    return cast(bytes, _packer().pack(pydantic_core.to_jsonable_python(data)))


def unpack_msgpack(raw: bytes) -> Any:
    """Unpack msgpack bytes into Python objects with strings decoded as str."""
    if msgpack is None:
        raise own_text(
            ImportError(
                "MessagePack serialization requires the 'msgpack' package. "
                "Install it with: pip install 'cliffracer[msgpack]'"
            )
        )
    return msgpack.unpackb(raw, raw=False)


ExtraForms = Literal["hierarchy", "caller checks", "none"]
"""Which forms `nested_form` offers a level beyond the dumps the caller's order always offers."""


class FormUnavailable(Exception):
    """A form `choose_wire_form` is offered cannot be made for this value; try the next."""


def choose_wire_form(
    value: Any,
    forms: Iterable[Callable[[], Any]],
    read: Callable[[Any], Any],
    faithful: Callable[[Any], bool] | None = None,
    *,
    alike: Callable[[Any, Any], bool] | None = None,
) -> Any:
    """The first of `forms` the receiver accepts and reads back as `value`.

    `forms` are zero-argument callables, each producing one JSON-ready spelling of `value`,
    tried in order and made only when reached. `read` is the receiver's validation. A form
    it refuses is not sent. A form it accepts but reads back as something else is the worst
    case, a value that arrives changed with nothing reporting it (two fields whose aliases
    are each other's names do this), so an accepted form that reads back equal wins over an
    accepted form that does not. Equal is `alike(back, value)`, by default `_reads_back_alike`: a
    model is judged by its class and the values of its fields, not by its own `__eq__`, which may
    compare fewer of them. When no form reads back equal, the first accepted form is
    sent: a model whose validator or serializer is not idempotent reads back changed in
    every spelling, and the call still goes out as it did. When none is accepted, the first
    form is sent and the receiver refuses it. A form that raises `FormUnavailable` is skipped.

    `faithful`, when given, is asked of what the receiver read from an accepted form that is not
    equal: whether it is what the receiver's validation makes of the caller's own values (a
    normalising validator's result). The first such form is preferred to the first accepted,
    so a normalised value arrives normalised and not as a field's default.

    A comparison that raises is "not equal".
    """
    first: Any = None
    made = False
    accepted: Any = None
    has_accepted = False
    faithful_form: Any = None
    has_faithful = False
    for make in forms:
        try:
            wire = make()
        except FormUnavailable:
            continue
        if not made:
            first, made = wire, True
        try:
            back = read(wire)
        except Exception:
            continue
        try:
            if (alike or _reads_back_alike)(back, value):
                return wire
        except Exception:
            pass
        if not has_accepted:
            accepted, has_accepted = wire, True
        if faithful is not None and not has_faithful:
            try:
                if faithful(back):
                    faithful_form, has_faithful = wire, True
            except Exception:
                pass
    if has_faithful:
        return faithful_form
    return accepted if has_accepted else first


def _accepts(cls: type[BaseModel], wire: Any) -> bool:
    """Whether `cls` accepts `wire`, read as the service reads a message."""
    try:
        read_python_then_json(wire, cls.model_validate, cls.model_validate_json)
    except Exception:
        return False
    return True


def _reads_back_alike(back: Any, value: Any) -> bool:
    """Whether `back`, what the receiver read from a form, is `value`: a model of the same class
    holding alike values in every field it declares, a dataclass the same way in its `compare=True`
    fields, a dict, list or tuple item by item, and anything else by `_same`.

    A model's own `__eq__` is not asked, since it may compare fewer fields (a record compared by
    its id) and call a form that changes the others equal. Its class is: a union member read back
    as another member with the same field values has arrived changed, which `_same`, comparing
    nested models by their field values alone, would not say."""
    if isinstance(value, BaseModel):
        return type(back) is type(value) and all(
            _reads_back_alike(getattr(back, name, None), getattr(value, name, None))
            for name in type(value).model_fields
        )
    if _is_dataclass_instance(value) and type(back) is type(value):
        return all(
            _reads_back_alike(getattr(back, f.name), getattr(value, f.name))
            for f in dataclasses.fields(value)
            if f.compare
        )
    if isinstance(back, dict) and isinstance(value, dict):
        return back.keys() == value.keys() and all(
            _reads_back_alike(back[k], value[k]) for k in value
        )
    if isinstance(back, list | tuple) and isinstance(value, list | tuple):
        return len(back) == len(value) and all(
            _reads_back_alike(x, y) for x, y in zip(back, value, strict=True)
        )
    return _same(back, value)


def _judged_by_fields(a: Any, b: Any) -> bool:
    """Whether `_same` compares `a` and `b` field by field rather than by `==`: two models, or two
    dataclasses of one class."""
    if isinstance(a, BaseModel) and isinstance(b, BaseModel):
        return True
    return _is_dataclass_instance(a) and type(a) is type(b)


def _same(a: Any, b: Any) -> bool:
    """`a == b`, with NaN equal to NaN, models compared by their field values whatever `__eq__` their
    class defines, dataclasses (standard or Pydantic) of one class compared by the values of their
    `compare=True` fields whatever `__eq__` the class defines (so an `eq=False` dataclass compares by
    value, not identity),
    and dicts, lists and tuples compared item by item the same way. A value read back from a NaN the
    caller sent is a NaN again, and `==` calls the two different.

    Two aware datetimes are the same when they are the same instant and neither is a wall time its
    own zone does not have. The wire carries an instant and an offset, so a value in a zone is read
    back in a fixed offset, and `==` between two zones is always False for a value in a repeated or
    skipped hour, whatever the instants. In one zone `==` compares wall times, so the two readings
    of a repeated hour are equal there although an hour apart; by instant they are not.

    A time whose zone gives it no offset (a `ZoneInfo`, which needs a date) is not the same as any
    value: it is written as its wall time alone, so its zone is lost, and `==` calls it equal to a
    naive time only because it treats it as one.

    A time whose offset has a sub-second part is not the same as any other value either: `==`
    drops that part, so it calls the time equal to the one the dump writes for it, whose offset
    has none."""
    if a is b:
        return True
    if _both_aware(a, b):
        if _wall_time_it_does_not_have(a) or _wall_time_it_does_not_have(b):
            return False
        try:
            return bool(a.astimezone(dt.UTC) == b.astimezone(dt.UTC))
        except (OverflowError, ValueError):
            pass
    try:
        # Not taken as the answer when either side holds a zone the wire loses, or an offset `==`
        # drops: `==` calls that value equal to what arrives, so it is compared item by item below.
        # Nor for two models, or two dataclasses of one class: they are judged by their fields
        # below, whatever `__eq__` the class defines.
        if (
            not _judged_by_fields(a, b)
            and a == b
            and _a_zone_the_wire_loses(a) is None
            and _a_zone_the_wire_loses(b) is None
            and not _an_offset_eq_drops(a)
            and not _an_offset_eq_drops(b)
        ):
            return True
    except Exception:
        return False
    if isinstance(a, float) and isinstance(b, float):
        return math.isnan(a) and math.isnan(b)
    if isinstance(a, BaseModel) and isinstance(b, BaseModel):
        # Field by field on the values as held, so a field holding a model is compared as one,
        # and a value of another kind that equals it is compared with the model itself.
        names = type(a).model_fields
        return names.keys() == type(b).model_fields.keys() and all(
            _same(getattr(a, name, None), getattr(b, name, None)) for name in names
        )
    if _is_dataclass_instance(a) and type(a) is type(b):
        return all(
            _same(getattr(a, f.name), getattr(b, f.name))
            for f in dataclasses.fields(a)
            if f.compare
        )
    if isinstance(a, dict) and isinstance(b, dict):
        return a.keys() == b.keys() and all(_same(a[k], b[k]) for k in a)
    if isinstance(a, list | tuple) and isinstance(b, list | tuple):
        return len(a) == len(b) and all(_same(x, y) for x, y in zip(a, b, strict=True))
    if isinstance(a, set | frozenset) and isinstance(b, set | frozenset):
        # A set compares its members by hash and `==`, neither of which follows the instant, so
        # each aware datetime is put in UTC first, as a field's value is judged by instant.
        if _a_zone_the_wire_loses(a) is not None or _a_zone_the_wire_loses(b) is not None:
            return False
        return _at_their_instants(a) == _at_their_instants(b)
    return False


def _at_their_instants(members: set[Any] | frozenset[Any]) -> frozenset[Any]:
    """`members` with each aware datetime in UTC, so a set compares them by instant, at any depth:
    a set or a tuple held as a member is compared by `==` and hash too."""
    return frozenset(_at_its_instant(member) for member in members)


def _at_its_instant(member: Any) -> Any:
    if isinstance(member, dt.datetime) and member.utcoffset() is not None:
        try:
            return member.astimezone(dt.UTC)
        except (OverflowError, ValueError):
            return member
    if isinstance(member, set | frozenset):
        return _at_their_instants(member)
    if isinstance(member, tuple):
        return tuple(_at_its_instant(item) for item in member)
    return member


def _both_aware(a: Any, b: Any) -> bool:
    return (
        isinstance(a, dt.datetime)
        and isinstance(b, dt.datetime)
        and a.utcoffset() is not None
        and b.utcoffset() is not None
    )


def _wall_time_it_does_not_have(value: Any) -> bool:
    """Whether `value` is an aware datetime whose zone has no such wall time (the hour skipped at
    the start of daylight saving time): its wall time does not survive a round trip through UTC in
    its own zone. A repeated hour survives it, each reading as itself."""
    if not isinstance(value, dt.datetime) or value.utcoffset() is None:
        return False
    try:
        back = value.astimezone(dt.UTC).astimezone(value.tzinfo)
    except (OverflowError, ValueError):
        return False
    return back.replace(tzinfo=None, fold=0) != value.replace(tzinfo=None, fold=0)


def _a_zone_a_time_cannot_carry(value: Any) -> bool:
    """Whether `value` is a time with a zone that gives it no offset: a `ZoneInfo` needs a date."""
    return isinstance(value, dt.time) and value.tzinfo is not None and value.utcoffset() is None


def _a_zone_the_wire_loses(value: Any) -> str | None:
    """How the first zoned value in `value` that the wire cannot carry would arrive, or None: an
    aware datetime whose zone has no such wall time, or a time whose zone gives it no offset. It is
    found in `value` itself, or held at any depth in a dict, list, tuple or set, or in a field of a
    model or dataclass."""
    if _wall_time_it_does_not_have(value):
        return _as_its_zone_shows_it(cast(dt.datetime, value))
    if _a_zone_a_time_cannot_carry(value):
        return _as_a_time_with_no_zone(cast(dt.time, value))
    for item in _held_in(value):
        found = _a_zone_the_wire_loses(item)
        if found is not None:
            return found
    return None


def _held_in(value: Any) -> Iterable[Any]:
    """The values `value` holds one level down: a dict's values, a list's, tuple's or set's
    members, or a model's or dataclass's fields."""
    if isinstance(value, dict):
        return value.values()
    if isinstance(value, list | tuple | set | frozenset):
        return value
    if isinstance(value, BaseModel):
        return [getattr(value, name, None) for name in type(value).model_fields]
    if _is_dataclass_instance(value):
        return [getattr(value, f.name, None) for f in dataclasses.fields(value)]
    return ()


def _an_offset_eq_drops(value: Any) -> bool:
    """Whether `value` is, or holds at any depth, a time whose offset has a sub-second part.

    `==` drops that part of a time's offset, so it calls the time equal to the one the dump writes
    for it, whose offset has none; such a value is compared item by item, by `_same`."""
    if isinstance(value, dt.time):
        offset = value.utcoffset()
        return offset is not None and offset.microseconds != 0
    return any(_an_offset_eq_drops(item) for item in _held_in(value))


def _as_its_zone_shows_it(value: dt.datetime) -> str:
    """`value`'s wall time, and the one its zone shows for the instant it stands for, named."""
    shown = value.astimezone(dt.UTC).astimezone(value.tzinfo)
    zone = _zone_name(value.tzinfo)
    return (
        f"{shown:%Y-%m-%d %H:%M} in {zone}, since {value:%H:%M} on that day is an hour "
        f"{zone} does not have"
    )


def _as_a_time_with_no_zone(value: dt.time) -> str:
    """`value`'s wall time as it would arrive, without the zone, named."""
    return (
        f"{value:%H:%M} with no zone, since {_zone_name(value.tzinfo)} gives a time no offset "
        "without a date"
    )


def _zone_name(zone: Any) -> str:
    """A zone's name for a message: a `ZoneInfo`'s key, else what the zone calls itself, else its
    repr. A `ZoneInfo` gives no name without a datetime to name it at, so its key is what names it;
    `str` of a zone of the caller's own is its object repr, so its own name is asked for first."""
    key = getattr(zone, "key", None)
    if isinstance(key, str) and key:
        return key
    try:
        name = zone.tzname(None)
    except Exception:
        name = None
    return name if isinstance(name, str) and name else repr(zone)


def _is_dataclass_instance(value: Any) -> bool:
    return dataclasses.is_dataclass(value) and not isinstance(value, type)


def _reads_as_the_argument(cls: type[BaseModel], wire: Any, value: BaseModel) -> bool:
    """Whether `cls` accepts `wire` and holds, in the fields it declares, the values `value` holds.

    `wire` is read as the service reads a message (`read_python_then_json`), so a strict model
    accepts the JSON form of its own dump here exactly where the service does.
    """
    try:
        back = read_python_then_json(wire, cls.model_validate, cls.model_validate_json)
    except Exception:
        return False
    try:
        # By each field's value, as the docstring says, and never by the model's own `__eq__`,
        # which may compare fewer fields (a record compared by its id) and call a form that
        # changes the others equal.
        return all(_same(getattr(back, name), getattr(value, name)) for name in cls.model_fields)
    except Exception:
        return False


def _classes_that_read_only(value: BaseModel, wanted: Any, other: Any) -> list[type[BaseModel]]:
    """The model classes of `type(value)` that read `wanted` as the argument and `other` not.

    The service's model is the handler's declared class, which is `type(value)` or one of its base
    classes, and a call has no annotation to say which. A class that reads both forms as the
    argument, or neither, cannot say which the handler wants: a base that holds only
    `model_config`, an empty mixin, a base whose fields all have defaults. Only a class that reads
    one form and not the other decides.
    """
    return [
        cls
        for cls in _model_classes(value)
        if _reads_as_the_argument(cls, wanted, value)
        and not _reads_as_the_argument(cls, other, value)
    ]


def _model_classes(value: BaseModel) -> list[type[BaseModel]]:
    """The model classes of `type(value)`, itself first: the classes a handler may declare for it."""
    return [
        cls for cls in type(value).__mro__ if issubclass(cls, BaseModel) and cls is not BaseModel
    ]


def _call_reader(
    value: BaseModel, classes: list[type[BaseModel]], own: type[BaseModel] | None = None
) -> Callable[[Any], Any]:
    """A reader for `choose_wire_form` that answers with `value` when any of `classes` reads the
    form as the argument, and with what `own` (by default `type(value)`) makes of it otherwise
    (raising if it refuses), each read as the service reads a message."""
    reader = own or type(value)

    def read(wire: Any) -> Any:
        if any(_reads_as_the_argument(cls, wire, value) for cls in classes):
            return value
        return read_python_then_json(wire, reader.model_validate, reader.model_validate_json)

    return read


def _choose_among(
    value: BaseModel,
    first: Any,
    make_second: Callable[[], Any],
    *more: Callable[[], Any],
    prefer_faithful: bool = True,
    declared: type[BaseModel] | None = None,
    first_refused: bool = False,
) -> Any:
    """The form of `value` to send, for a model whose declared class is not known, or is
    `declared`.

    `first` is the form that was always sent. When the instance's own class reads it as the
    argument it is sent, for the price of one validation. Otherwise the instance's own class may
    not be the handler's declared class: a base class decides only where it reads `first` as the
    argument and the second form not (`_classes_that_read_only`), and when none does the
    instance's own class reads. The forms, then any `more`, are offered to `choose_wire_form` in
    order, preferring, when `prefer_faithful`, a form the instance's class reads as its validators
    make of the caller's values to the first accepted. With `declared`, that class alone reads.
    `first_refused` says the caller has already found the class does not read `first` as the
    argument, so that check is not made again.
    """
    own = declared or type(value)
    if not first_refused and _reads_as_the_argument(own, first, value):
        return first
    second = make_second()
    if declared is not None:
        classes = [declared]
    else:
        classes = _classes_that_read_only(value, first, second) or [type(value)]
    forms = (lambda: first, lambda: second, *more)
    faithful = faithful_to(own, value) if prefer_faithful else None
    return choose_wire_form(value, forms, _call_reader(value, classes, own), faithful)


def _validation_path(field: FieldInfo) -> list[str | int] | None:
    """Where a field is read from when its `validation_alias` is what the model reads: the key, the
    first `AliasChoices` member, or an `AliasPath`'s steps. None for a field with none."""
    alias = field.validation_alias
    if isinstance(alias, AliasChoices):
        alias = alias.choices[0] if alias.choices else None
    if isinstance(alias, AliasPath):
        return list(alias.path)
    if isinstance(alias, str):
        return [alias]
    return None


def _put(node: Any, path: list[str | int], value: Any) -> None:
    """Write `value` at `path` in `node`, making the dicts and lists the steps name."""
    head, rest = path[0], path[1:]
    if isinstance(head, int):
        while len(node) <= head:
            node.append(None)
    if not rest:
        node[head] = value
        return
    child = node[head] if isinstance(head, int) else node.get(head)
    if not isinstance(child, list | dict):
        child = [] if isinstance(rest[0], int) else {}
        node[head] = child
    _put(child, rest, value)


def _under_validation_aliases(cls: type[BaseModel], dumped: Any) -> Any:
    """`dumped`, one model's dump by field name, with each field that has a `validation_alias` moved
    to where that alias reads it (`_validation_path`).

    A dump writes a field's name, or its serialization alias, never its validation alias, so a model
    read only through `AliasChoices` or `AliasPath` read the dump as the field's default. A field
    with a plain `alias` has that alias as its validation alias, so it moves too, and a field with
    none stays under its name, which is how it is read.

    Raises `FormUnavailable` when the model has no field to move: the form would be the dump by
    field name, which every path already offers.
    """
    moves = [
        (name, path)
        for name, field in cls.model_fields.items()
        if (path := _validation_path(field)) is not None
    ]
    if not moves or not isinstance(dumped, dict):
        raise FormUnavailable(f"{cls.__name__} reads no validation alias")
    out = dict(dumped)
    for name, path in moves:
        if name in out:
            _put(out, path, out.pop(name))
    return out


def _declares_serializers(cls: type[BaseModel]) -> bool:
    """Whether `cls` declares something that runs when a model is dumped.

    A field serializer, a model serializer or a computed field is handed the model's own
    attributes, and `nested_form` hands it a copy whose nested models are already dicts.
    """
    decorators = cls.__pydantic_decorators__
    return bool(
        decorators.field_serializers or decorators.model_serializers or decorators.computed_fields
    )


def nested_form(
    value: Any,
    *,
    alias_first: bool,
    extra_forms: ExtraForms = "hierarchy",
    declared: type[BaseModel] | None = None,
) -> Any:
    """`value` written one model at a time, each level in the form its own class reads back.

    A whole-value dump has one `by_alias` switch, so an outer model read by field name that
    holds an inner model read by alias has no dump the service accepts. Here each model is
    written after the models inside it: they are replaced by their own JSON-ready form, the
    outer is dumped as a copy holding those, and the outer's form is the first its class
    reads back equal (`choose_wire_form`; by alias first when `alias_first`). Containers are
    walked; values that are not models are left for pydantic's JSON serialisation.

    Raises `FormUnavailable` when a model on the way declares a serializer or a computed
    field (see `_declares_serializers`), or carries extra fields: the copy would hand them
    something other than the caller's model, so the other forms stand. A serializer written in
    `Annotated` is not seen there. One that raises on the copy, or any other error while the
    form is built, also makes it unavailable; one that writes something the class does not read
    back as the argument is refused by the read-back check.

    `extra_forms` says what a level is offered beyond the dumps its order always offers (by alias
    and by field name; the model's default dump and by alias on `_encode`'s order):
    - "hierarchy", for the rpc call paths, which do not know the receiver's model: the form with
      each field written where its validation alias reads it (`_under_validation_aliases`), where
      no model class of the instance's hierarchy accepts it without reading it faithfully
      (`_reads_faithfully`), or reads a dump offered before it faithfully and not it;
    - "caller checks", for `_encode`, which reads the result with the declared annotation and
      chooses again with "none" where that does not read it faithfully: the dump by field name and
      the validation-alias form, unchecked;
    - "none": nothing, and a level's form is the first its class reads back as the argument, else
      the first accepted.
    With the first two, a form the instance's class reads as its own validators make of the
    caller's values (a normalising validator) is preferred to the first accepted.

    `declared`, the caller's annotation when it is a model class `value` is an instance of, reads
    the outermost level in place of the instance's own class; the levels inside it are read by
    their own classes.
    """
    try:
        return _nested_form(value, alias_first, extra_forms, declared)
    except FormUnavailable:
        raise
    except Exception as exc:
        raise FormUnavailable(f"{type(value).__name__}: {type(exc).__name__}") from exc


def _nested_form(
    value: Any,
    alias_first: bool,
    extra_forms: ExtraForms,
    declared: type[BaseModel] | None = None,
) -> Any:
    """The body of `nested_form`, which turns anything it raises into `FormUnavailable`."""
    if isinstance(value, BaseModel):
        cls = type(value)
        if _declares_serializers(cls) or value.__pydantic_extra__:
            raise FormUnavailable(cls.__name__)
        shallow = value.model_copy()
        wired = {
            name: _wired(getattr(value, name), alias_first, extra_forms)
            for name in cls.model_fields
        }
        shallow.__dict__.update(wired)

        def dump(by_alias: bool | None) -> Any:
            written = shallow.model_dump(mode="json", by_alias=by_alias, warnings=False)
            # Dumping this level writes again, under this level's config, what the models below it
            # already wrote under theirs: a NaN a constants model kept comes back as None. Where a
            # field holds a model, at any depth, that model's own form is put back.
            by_key = (
                by_alias
                if by_alias is not None
                else bool(cls.model_config.get("serialize_by_alias"))
            )
            for name, field in cls.model_fields.items():
                key = (field.serialization_alias or name) if by_key else name
                if key in written:
                    written[key] = _levels_kept(getattr(value, name), wired[name], written[key])
            return written

        # The orders the two callers use for a whole model: `_encode` tries the model's own
        # default, then by alias, then (unless "none") by name; the rpc call paths try by alias
        # first, then by name.
        offered: tuple[Callable[[], Any], ...]
        if alias_first:
            offered = (lambda: dump(True), lambda: dump(False))
        elif extra_forms == "none":
            offered = (lambda: dump(None), lambda: dump(True))
        else:
            offered = (lambda: dump(None), lambda: dump(True), lambda: dump(False))

        # Last, the form that writes each field where its validation alias reads it. A class
        # "reads it faithfully" when it reads it as the argument, or as its own validators make of
        # the caller's values (a normalising validator, `_reads_faithfully`).
        #
        # With "hierarchy" the handler may declare any model class of the instance's hierarchy, and
        # the form is withheld when one of them does not read it faithfully and
        # - accepts it: that class would read other values (a base reading `x` by name gets its
        #   default from `{"xx": 5}`; two fields read from one key) where the dumps were read, or
        #   were refused and the receiver said so;
        # - or reads a dump offered before it faithfully: that dump is what was sent before.
        # So where it is sent, each class reads it faithfully or refuses it, and a class that
        # refuses it read none of the dumps offered before it faithfully. A class that reads every
        # form as the argument (a base holding only `model_config`, or only defaulted fields) says
        # nothing.
        #
        # With "caller checks" it is offered as it is: the caller reads the result with the declared
        # annotation, and chooses again with "none" where that does not read it faithfully.
        def under_aliases() -> Any:
            form = _under_validation_aliases(cls, dump(False))
            for k in _model_classes(value) if extra_forms == "hierarchy" else ():
                if _reads_faithfully(k, form, value):
                    continue
                if _accepts(k, form):
                    raise FormUnavailable(f"{k.__name__} reads this form as other values")
                if any(_reads_faithfully(k, make(), value) for make in offered):
                    raise FormUnavailable(
                        f"{k.__name__} reads a form offered earlier, not this one"
                    )
            return form

        if declared is not None and not isinstance(value, declared):
            declared = None
        if extra_forms == "none":
            return _choose_among(
                value, offered[0](), offered[1], prefer_faithful=False, declared=declared
            )
        return _choose_among(
            value, offered[0](), offered[1], *offered[2:], under_aliases, declared=declared
        )
    return pydantic_core.to_jsonable_python(_wired(value, alias_first, extra_forms))


def _levels_kept(original: Any, wired: Any, written: Any) -> Any:
    """`written`, a level's dump of one field, with each position that held a model replaced by
    `wired`, that model's own nested form, which its own config wrote."""
    if isinstance(original, BaseModel):
        return wired
    if isinstance(original, dict) and isinstance(written, dict):
        return {
            key: _levels_kept(item, wired_item, written_item)
            for (key, written_item), item, wired_item in zip(
                written.items(), original.values(), wired.values(), strict=True
            )
        }
    if isinstance(original, list | tuple | set | frozenset) and isinstance(written, list):
        return [
            _levels_kept(item, wired_item, written_item)
            for item, wired_item, written_item in zip(original, wired, written, strict=True)
        ]
    return written


def _wired(value: Any, alias_first: bool, extra_forms: ExtraForms) -> Any:
    """`value` with each model in it replaced by its `nested_form`; the rest left as it is."""
    if isinstance(value, BaseModel):
        return nested_form(value, alias_first=alias_first, extra_forms=extra_forms)
    if isinstance(value, dict):
        return {key: _wired(item, alias_first, extra_forms) for key, item in value.items()}
    if isinstance(value, list | tuple | set | frozenset):
        return [_wired(item, alias_first, extra_forms) for item in value]
    return value


def _expected(cls: type[BaseModel], value: BaseModel) -> BaseModel | None:
    """What `cls` makes of `value`'s own field values, read by field name: the caller's values as
    the receiver's validators leave them. None when `cls` cannot read them so."""
    try:
        return cls.model_validate(
            {name: getattr(value, name) for name in cls.model_fields if hasattr(value, name)},
            by_alias=False,
            by_name=True,
        )
    except Exception:
        return None


def _dump_key(field_name: str, field: FieldInfo, by_alias: bool) -> str:
    """The key a dump writes a field under, by alias or by field name."""
    if not by_alias:
        return field_name
    return field.serialization_alias or field.alias or field_name


def _expected_from_its_dumps(
    cls: type[BaseModel], value: BaseModel
) -> tuple[BaseModel | None, set[str]]:
    """What `cls` makes of `value`'s own dump, its serializers applied, and the fields it speaks for:
    those its dump by alias and its dump by field name write alike. A serializer that writes a field
    one way by alias and another by field name says nothing about it, and neither does a dump that
    holds one value for two fields under one key. A nested model is left as the caller's: it is
    compared a level down. None when `cls` cannot read it so."""
    try:
        by_alias = value.model_dump(by_alias=True, warnings=False)
        by_name = value.model_dump(by_alias=False, warnings=False)
        own = type(value).model_fields
        keys = [_dump_key(name, field, True) for name, field in own.items() if not field.exclude]
        data: dict[str, Any] = {}
        speaks_for: set[str] = set()
        for name in cls.model_fields:
            if name not in own or own[name].exclude:
                continue
            held = getattr(value, name, None)
            if isinstance(held, BaseModel):
                data[name] = held
                continue
            key = _dump_key(name, own[name], True)
            if keys.count(key) > 1:
                continue
            if name in by_name and key in by_alias and _same(by_name[name], by_alias[key]):
                data[name] = by_name[name]
                speaks_for.add(name)
        return cls.model_validate(data, by_alias=False, by_name=True), speaks_for
    except Exception:
        return None, set()


def faithful_to(cls: type[BaseModel], value: BaseModel) -> Callable[[Any], bool] | None:
    """A test, for `choose_wire_form`, of whether a model `cls` read from a form holds what `cls`
    makes of `value`'s own field values (`_expected`). None when `cls` cannot read them so."""
    expected = _expected(cls, value)
    if expected is None:
        return None

    def faithful(read: Any) -> bool:
        if read is value:
            return True
        if not isinstance(read, BaseModel):
            return False
        return all(_same(getattr(read, n, None), getattr(expected, n)) for n in cls.model_fields)

    return faithful


def _reads_faithfully(cls: type[BaseModel], wire: Any, value: BaseModel) -> bool:
    """Whether `cls` reads `wire` as the argument, or as its own validators make of `value`'s field
    values (a normalising validator's result: `faithful_to`)."""
    if _reads_as_the_argument(cls, wire, value):
        return True
    faithful = faithful_to(cls, value)
    if faithful is None:
        return False
    try:
        read = read_python_then_json(wire, cls.model_validate, cls.model_validate_json)
        return faithful(read)
    except Exception:
        return False


def _field_values(value: Any) -> Any:
    """A model's field values by name, nested models included; anything else as it is."""
    if isinstance(value, BaseModel):
        return {
            name: _field_values(getattr(value, name, None)) for name in type(value).model_fields
        }
    return value


def _holds_the_values_of(mine: Any, other: Any) -> bool:
    """Whether `mine`, a nested model's field values, equal `other`'s in every field the two share,
    and they share at least one: what a model read from another field's key holds."""
    theirs = _field_values(other)
    if not isinstance(mine, dict) or not isinstance(theirs, dict):
        return False
    shared = [name for name in mine if name in theirs]
    return bool(shared) and all(_same(mine[name], theirs[name]) for name in shared)


def misread_values(
    cls: type[BaseModel], read: BaseModel, value: BaseModel, at: str = ""
) -> list[tuple[str, str | None]]:
    """The fields of `value` that `read`, the model a receiver built from what was sent, holds as
    something other than what the caller set: `(field, None)` for a field read as its default, and
    `(field, how)` for any other value, where `how` names it (`"b's value"` for the value the caller
    set on field `b`, `"another value"` otherwise).

    A field read as `value` holds it, as `cls`'s validators make of the caller's values, or as they
    make of the model's own dump of them where the dump by alias and the dump by field name agree on
    it (a serializer that writes a value other than the one it holds, in every dump), is read right;
    anything else is counted. A serializer that writes a field one way by alias and another by field
    name says nothing about it: either dump may match a misread of the form written the other way
    (`_expected_from_its_dumps`). A nested model read with the values the caller set on another field, in the fields the two share (the outer
    field's alias is the other field's name), is named as that field; otherwise nested models are
    compared field by field. A required field has no default, so it is never counted as read as one.
    A field excluded from dumps is never sent, by the model's own declaration, so it is not counted.
    """
    expected = _expected(cls, value)
    dumped, speaks_for = _expected_from_its_dumps(cls, value)
    found: list[tuple[str, str | None]] = []
    for name, field in cls.model_fields.items():
        if field.exclude:
            continue
        sent, got = getattr(value, name, None), getattr(read, name, None)
        here = f"{at}{name}"
        if isinstance(sent, BaseModel) and isinstance(got, BaseModel):
            mine = _field_values(got)
            right = [_field_values(sent)]
            if expected is not None:
                right.append(_field_values(getattr(expected, name, None)))
            if any(_same(mine, one) for one in right):
                continue
            crossed = next(
                (
                    other
                    for other, other_field in cls.model_fields.items()
                    if other != name
                    and not other_field.exclude
                    and _holds_the_values_of(mine, getattr(value, other, None))
                ),
                None,
            )
            if crossed is not None:
                found.append((here, f"{at}{crossed}'s value"))
                continue
            found += misread_values(type(got), got, sent, f"{here}.")
            continue
        try:
            lost = _a_zone_the_wire_loses(sent)
            if lost is not None:
                found.append((here, lost))
                continue
            if (
                _same(got, sent)
                or (expected is not None and _same(got, getattr(expected, name)))
                or (dumped is not None and name in speaks_for and _same(got, getattr(dumped, name)))
            ):
                continue
            if not field.is_required() and _same(got, field.get_default(call_default_factory=True)):
                found.append((here, None))
                continue
            crossed = next(
                (
                    other
                    for other, other_field in cls.model_fields.items()
                    if other != name
                    and not other_field.exclude
                    and _same(got, getattr(value, other, None))
                ),
                None,
            )
            found.append((here, f"{at}{crossed}'s value" if crossed else "another value"))
        except Exception:
            continue
    return found


def refuse_a_lost_value(
    cls: type[BaseModel], form: Any, value: BaseModel, at: tuple[int | str, ...] = ()
) -> None:
    """Raise `RpcValidationError` when `cls` reads `form` with a field the caller set read as
    anything other than what `cls`'s validators make of the caller's value.

    `at` is where the model sits in the argument, the index or key of each container around it, and
    leads each detail's `loc` and follows the class's name in the message.

    The chooser falls back to a form no class reads back as the argument; for a model whose
    validator normalises a value that is right, and the call goes as it did. But a field read as its
    default, as another field's value, or as any other value in place of what the caller set is not
    normalisation, it is the value lost, so the call is refused before it is sent, naming the
    fields. A form the class refuses is left for the receiver to refuse.
    """
    try:
        read = read_python_then_json(form, cls.model_validate, cls.model_validate_json)
    except Exception:
        return
    misread = misread_values(cls, read, value)
    if not misread:
        return
    details: list[dict[str, Any]] = []
    for field, how in misread:
        if how is None:
            details.append(
                {
                    "type": "value_would_be_lost",
                    "loc": [*at, field],
                    "msg": "no form of the model is read back with this field's value; it would "
                    "arrive as its default",
                }
            )
        else:
            details.append(
                {
                    "type": "value_would_be_misread",
                    "loc": [*at, field],
                    "msg": "no form of the model is read back with this field's value; it would "
                    f"arrive holding {how}",
                }
            )
    said = ", ".join(
        f"{field} read as its default" if how is None else f"{field} read as {how}"
        for field, how in misread
    )
    raise RpcValidationError(
        details=details,
        message=(
            f"refused before sending: {cls.__name__}{''.join(f'[{k!r}]' for k in at)} would arrive "
            f"with {said}, and no form of it is read back as the argument"
        ),
    )


#: Config keys that change neither how a model validates a dict nor how it dumps to JSON, so a class
#: may set them to anything and still be plain. Every other key a class's config holds must have
#: pydantic's default value. `use_enum_values` changes only what a model holds for an enum field,
#: the member's value where the member was, and the enum check takes that value.
_INERT_CONFIG = frozenset(
    {
        "title",
        "frozen",
        "validate_assignment",
        "hide_input_in_errors",
        "use_attribute_docstrings",
        "protected_namespaces",
        "defer_build",
        "model_title_generator",
        "field_title_generator",
        "json_schema_mode_override",
        "json_schema_serialization_defaults_required",
        "plugin_settings",
        "use_enum_values",
    }
)
#: Field options that change neither validation nor the dump. A field that sets any other option,
#: or carries constraint metadata, makes its class not plain.
_INERT_FIELD = frozenset(
    {
        "annotation",
        "default",
        "default_factory",
        "description",
        "title",
        "examples",
        "frozen",
        "repr",
    }
)

_config_defaults: Mapping[str, Any] | None
try:
    from pydantic._internal._config import config_defaults as _pydantic_config_defaults

    _config_defaults = dict(_pydantic_config_defaults)
except ImportError:  # pragma: no cover - a pydantic without it: no class is plain
    _config_defaults = None

_Check = tuple[tuple[tuple[str, type], ...], tuple[tuple[str, Callable[[Any], bool]], ...]]
_plain_classes: "weakref.WeakKeyDictionary[type, _Check | None]" = weakref.WeakKeyDictionary()


def _is_finite_float(value: Any) -> bool:
    return type(value) is float and math.isfinite(value)


def _is_encodable_str(value: Any) -> bool:
    """Exactly a `str`, and one that encodes as UTF-8 (no lone surrogate)."""
    if type(value) is not str:
        return False
    if value.isascii():
        return True
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def _has_a_whole_minute_offset_or_none(value: dt.datetime | dt.time) -> bool:
    """No zone, or exactly a `datetime.timezone` whose offset is whole minutes.

    The dump writes an offset to the minute, so a finer one is read back as another value. Any
    other `tzinfo` (a `ZoneInfo` among them) takes the read-back, which judges a datetime by its
    instant and refuses one whose wall time its zone does not have, and refuses a time whose zone
    gives it no offset."""
    zone = value.tzinfo
    if zone is None:
        return True
    if type(zone) is not dt.timezone:
        return False
    offset = value.utcoffset()
    return offset is not None and offset.microseconds == 0 and offset.seconds % 60 == 0


def _is_plain_datetime(value: Any) -> bool:
    return type(value) is dt.datetime and _has_a_whole_minute_offset_or_none(value)


def _is_plain_time(value: Any) -> bool:
    return type(value) is dt.time and _has_a_whole_minute_offset_or_none(value)


#: The checks for the temporal types: exactly the type, and for a datetime or a time a zone whose
#: dump is read back as the same value.
_TEMPORAL_CHECKS: dict[Any, Callable[[Any], bool]] = {
    dt.datetime: _is_plain_datetime,
    dt.date: lambda value: type(value) is dt.date,
    dt.time: _is_plain_time,
    dt.timedelta: lambda value: type(value) is dt.timedelta,
}


def _is_utf8_bytes(value: Any) -> bool:
    """Exactly `bytes`, and bytes that decode as UTF-8, which is how the dump writes them."""
    if type(value) is not bytes:
        return False
    try:
        value.decode("utf-8")
    except UnicodeDecodeError:
        return False
    return True


def _enum_check(annotation: type[enum.Enum]) -> Callable[[Any], bool]:
    """A member of exactly `annotation`, or a value exactly one of its members holds (what a
    model holds under `use_enum_values`, or by `model_construct`): the dump writes the value, and
    the class reads it back as that member."""
    values = [(type(member.value), member.value) for member in annotation]

    def check(value: Any) -> bool:
        if type(value) is annotation:
            return True
        return any(type(value) is kind and value == held for kind, held in values)

    return check


#: The scalar types a two-type union of the plain grammar may hold, with their checks.
_UNION_SCALARS: dict[Any, Callable[[Any], bool]] = {
    str: _is_encodable_str,
    int: lambda value: type(value) is int,
    bool: lambda value: type(value) is bool,
    float: _is_finite_float,
}


def _instance_check(annotation: Any, visiting: set[type]) -> Callable[[Any], bool] | None:
    """A check that a value is exactly of `annotation`'s plain shape, or None if it has none."""
    if annotation is str:
        return _is_encodable_str
    if annotation is int or annotation is bool:
        return lambda value: type(value) is annotation
    if annotation in _TEMPORAL_CHECKS:
        return _TEMPORAL_CHECKS[annotation]
    if annotation is decimal.Decimal:
        return lambda value: type(value) is decimal.Decimal
    if annotation is float:
        return _is_finite_float
    if annotation is bytes:
        return _is_utf8_bytes
    if isinstance(annotation, type) and issubclass(annotation, enum.Enum):
        return _enum_check(annotation)
    origin, args = typing.get_origin(annotation), typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        members = [arg for arg in args if arg is not type(None)]
        if len(members) == 2 and all(member in _UNION_SCALARS for member in members):
            # A union of two scalar types reads its dump back as the type the value holds:
            # pydantic's smart mode takes an exact type match first. One holding `bytes` is not
            # one: `str | bytes` reads bytes back as `str`.
            either = [_UNION_SCALARS[member] for member in members]
            allows_none = len(args) == 3
            return lambda value: (allows_none and value is None) or any(
                check(value) for check in either
            )
    if origin in (typing.Union, types.UnionType) and len(args) == 2 and type(None) in args:
        found = _instance_check(args[0] if args[1] is type(None) else args[1], visiting)
        if found is None:
            return None
        inner: Callable[[Any], bool] = found
        return lambda value: value is None or inner(value)
    if origin is list and len(args) == 1:
        found = _instance_check(args[0], visiting)
        if found is None:
            return None
        member: Callable[[Any], bool] = found
        return lambda value: type(value) is list and all(member(each) for each in value)
    if origin is tuple and len(args) == 2 and args[1] is Ellipsis:
        found = _instance_check(args[0], visiting)
        if found is None:
            return None
        each_member: Callable[[Any], bool] = found
        return lambda value: type(value) is tuple and all(each_member(each) for each in value)
    if origin in (set, frozenset) and len(args) == 1:
        found = _instance_check(args[0], visiting)
        if found is None:
            return None
        set_member: Callable[[Any], bool] = found
        kind = origin
        # The dump lists a set's members in the order it iterates, which for `str` members follows
        # the process's hash seed; the service reads the same set whatever the order.
        return lambda value: type(value) is kind and all(set_member(each) for each in value)
    if origin is tuple and Ellipsis not in args:
        # A fixed tuple, `tuple[()]` included: exactly that many members, each of its position's
        # type. One of another length is refused by the service whatever form it is sent in.
        positions: list[Callable[[Any], bool]] = []
        for position in args:
            found = _instance_check(position, visiting)
            if found is None:
                return None
            positions.append(found)
        return lambda value: (
            type(value) is tuple
            and len(value) == len(positions)
            and all(check(each) for check, each in zip(positions, value, strict=True))
        )
    if origin is dict and len(args) == 2 and args[0] is str:
        found = _instance_check(args[1], visiting)
        if found is None:
            return None
        entry: Callable[[Any], bool] = found
        return lambda value: type(value) is dict and all(
            _is_encodable_str(key) and entry(each) for key, each in value.items()
        )
    if isinstance(annotation, type) and issubclass(annotation, BaseModel):
        if _plain_check(annotation, visiting) is None:
            return None
        return lambda value: type(value) is annotation and _is_plain_instance(value)
    return None


def _plain_check(cls: type[BaseModel], visiting: set[type] | None = None) -> _Check | None:
    """For a plain model class, the checks its instances' fields must pass; None otherwise.

    A class is plain when no form but its alias dump can matter for it, which a model of it then
    reads back as the value whenever every value it holds is exactly of its field's plain type: no
    alias of any kind, no validator, serializer or computed field (pydantic's resolved records, so
    an inherited one counts), no `model_post_init`, `__eq__` or schema hook of its own, a config
    whose keys are inert or at their default, and fields of `str`, `int`, `bool`, `float`,
    `Decimal`, `datetime`, `date`, `time`, `timedelta`, `bytes`, an `Enum`, a union of two of `str`,
    `int`, `bool` and `float`, `X | None`, `list[X]`, `tuple[X, ...]`, a fixed tuple of those,
    `set[X]`, `frozenset[X]`, `dict[str, X]` or a plain model, with no option that validates or
    dumps. A field of `Any`, or of a union holding `bytes`, is not plain: what it holds decides how
    it reads back.
    The answer is kept per class once pydantic has finished building it. A class that refers to
    itself is not plain."""
    try:
        return _plain_classes[cls]
    except KeyError:
        pass
    visiting = set() if visiting is None else visiting
    if cls in visiting:
        return None
    visiting.add(cls)
    try:
        check = _plain_check_of(cls, visiting)
    except Exception:
        # Pydantic attributes read below are not public API; a class this cannot read is not plain,
        # and takes the read-back, rather than every publish raising.
        check = None
    finally:
        visiting.discard(cls)
    if getattr(cls, "__pydantic_complete__", False):
        _plain_classes[cls] = check
    return check


def _plain_check_of(cls: type[BaseModel], visiting: set[type]) -> _Check | None:
    if _config_defaults is None or not getattr(cls, "__pydantic_complete__", False):
        return None
    if issubclass(cls, RootModel) or getattr(cls, "__pydantic_root_model__", True):
        return None
    own = [k for k in cls.__mro__ if k is not BaseModel and issubclass(k, BaseModel)]
    for name in ("__eq__", "model_post_init", "__get_pydantic_core_schema__"):
        if any(name in vars(k) for k in own):
            return None
    decorators = getattr(cls, "__pydantic_decorators__", None)
    if decorators is None:
        return None
    if any(
        (
            decorators.validators,
            decorators.field_validators,
            decorators.root_validators,
            decorators.field_serializers,
            decorators.model_serializers,
            decorators.model_validators,
            decorators.computed_fields,
            cls.model_computed_fields,
        )
    ):
        return None
    for key, setting in cls.model_config.items():
        if key not in _INERT_CONFIG and (
            key not in _config_defaults or setting != _config_defaults[key]
        ):
            return None
    scalars: list[tuple[str, type]] = []
    others: list[tuple[str, Callable[[Any], bool]]] = []
    for name, field in cls.model_fields.items():
        attributes = getattr(field, "_attributes_set", None)
        if attributes is None or field.metadata or not set(attributes) <= _INERT_FIELD:
            return None
        annotation = field.annotation
        if annotation is int or annotation is bool:
            scalars.append((name, annotation))
            continue
        check = _instance_check(annotation, visiting)
        if check is None:
            return None
        others.append((name, check))
    return tuple(scalars), tuple(others)


_ABSENT = object()


def _is_plain_instance(value: BaseModel) -> bool:
    """Whether `value` is of a plain class and holds, at every depth, exactly its fields' types.

    Exactly: `type(v) is int` (no bool), a float that is finite, a str that encodes as UTF-8, a
    `Decimal` (NaN and the infinities included: each is written as its text), a datetime or time
    with no zone or a `datetime.timezone` of whole minutes, a date that is not a datetime, a tuple
    that is exactly a `tuple` (of the declared length, for a fixed one), a set or a frozenset that
    is exactly the one declared, `bytes` that decode as UTF-8, a member of exactly the declared enum
    or a value one of its members holds, a value one of a union's two types takes, a nested model
    of exactly the declared class, and a field that is present. A value
    this turns away takes the read-back. For a datetime or time in another zone that decides the
    outcome: the read-back refuses a datetime whose offset is finer than a minute, a time whose
    offset is a whole number of seconds finer than a minute, a datetime whose wall time its zone
    does not have, and a time whose zone gives it no offset. For the rest none is sent differently
    by the walk today, so it is a guard against a later rule that reads such a value."""
    check = _plain_check(type(value))
    if check is None or getattr(value, "__pydantic_extra__", True):
        return False
    held = value.__dict__
    scalars, others = check
    for name, kind in scalars:
        if type(held.get(name, _ABSENT)) is not kind:
            return False
    for name, test in others:
        if name not in held or not test(held[name]):
            return False
    return True


def _where_the_dump_fails(model: BaseModel, path: str) -> tuple[str, str]:
    """Where the JSON dump of `model` fails, as a path, and the name of the model holding it.

    Each declared and computed field is dumped alone. The model itself is named when the failure
    is the model's: it declares a model serializer that fails with no field included, so every
    field would read as failing. A wrap-mode serializer that hands the dump on fails only on what
    it is handed, and the field is named. Otherwise the first field that fails is named, or the
    model in it whose dump fails, followed through lists, tuples and dict values to its index or
    key."""
    cls = type(model)
    names = (*cls.model_fields, *cls.model_computed_fields)
    failing = []
    for name in names:
        try:
            model.model_dump(mode="json", include={name})
        except Exception:
            failing.append(name)
    if not failing or (cls.__pydantic_decorators__.model_serializers and _fails_whole(model)):
        return path, cls.__name__
    where = f"{path}[{failing[0]!r}]"
    try:
        value = getattr(model, failing[0])
    except Exception:
        return where, cls.__name__
    return _a_failing_model_in(value, where) or (where, cls.__name__)


def _fails_whole(model: BaseModel) -> bool:
    """Whether `model`'s dump fails with no field included: its model serializer fails by itself."""
    try:
        model.model_dump(mode="json", include=set())
    except Exception:
        return True
    return False


def _a_failing_model_in(value: Any, path: str) -> tuple[str, str] | None:
    """The first model in `value` (itself, or held in lists, tuples and dict values) whose JSON dump
    fails, located by `_where_the_dump_fails`; None when there is none."""
    if isinstance(value, BaseModel):
        try:
            value.model_dump(mode="json")
        except Exception:
            return _where_the_dump_fails(value, path)
        return None
    if isinstance(value, list | tuple):
        members: Iterable[tuple[Any, Any]] = enumerate(value)
    elif isinstance(value, dict):
        members = value.items()
    else:
        return None
    for key, member in members:
        found = _a_failing_model_in(member, f"{path}[{key!r}]")
        if found is not None:
            return found
    return None


def with_subclass_fields_at_open_bases(value: Any, back: Any, form: Any) -> Any:
    """`form` with each model subclass instance's own fields written in where the class declared
    for it allows extras, so the instance goes whole and the receiver reads them as its extras.

    `back` is what the receiver reads from `form`, so its models are the declared classes. Where a
    subclass instance stands at a declared base with `extra="allow"`, the fields the base does not
    declare are added as the subclass's own `model_dump` writes them (by field name, unless its
    config serializes by alias), never over a key the form already has. A base that ignores or forbids extras could not hold them, and gets the form unchanged.
    Lists, tuples and dicts are walked item by item; anything else is returned as it is. `form` is
    returned itself when nothing is added.
    """
    if isinstance(value, BaseModel) and isinstance(back, BaseModel) and isinstance(form, dict):
        declared = type(back)
        written = dict(form)
        for name, field in declared.model_fields.items():
            keys = (field.serialization_alias, field.alias, name)
            key = next((k for k in keys if k is not None and k in written), None)
            if key is not None:
                written[key] = with_subclass_fields_at_open_bases(
                    getattr(value, name, None), getattr(back, name, None), written[key]
                )
        own = set(type(value).model_fields) - set(declared.model_fields)
        if own and isinstance(value, declared) and declared.model_config.get("extra") == "allow":
            for key, item in value.model_dump(mode="json", include=own).items():
                written.setdefault(key, item)
        return form if written == form else written
    if (
        isinstance(value, list | tuple)
        and isinstance(back, list | tuple)
        and isinstance(form, list)
        and len(value) == len(back) == len(form)
    ):
        items = [
            with_subclass_fields_at_open_bases(v, b, f)
            for v, b, f in zip(value, back, form, strict=True)
        ]
        return form if all(i is f for i, f in zip(items, form, strict=True)) else items
    if (
        isinstance(value, dict)
        and isinstance(back, dict)
        and isinstance(form, dict)
        and len(value) == len(back) == len(form)
    ):
        pairs = {
            key: with_subclass_fields_at_open_bases(v, b, f)
            for (key, f), v, b in zip(form.items(), value.values(), back.values(), strict=True)
        }
        return form if all(pairs[k] is form[k] for k in form) else pairs
    return form


def _holds_a_subclass_at_an_open_base(value: Any) -> bool:
    """Whether a model held in `value` (not `value` itself) is a subclass instance that declares
    fields beyond an `extra="allow"` model class it descends from: the only shape
    `with_subclass_fields_at_open_bases` changes, so the read-back it needs is paid for no other."""

    def held(item: Any) -> bool:
        if isinstance(item, BaseModel):
            own = set(type(item).model_fields)
            for base in type(item).__mro__[1:]:
                if (
                    isinstance(base, type)
                    and issubclass(base, BaseModel)
                    and base is not BaseModel
                    and base.model_config.get("extra") == "allow"
                    and own - set(base.model_fields)
                ):
                    return True
            return any(held(getattr(item, name, None)) for name in type(item).model_fields)
        if isinstance(item, dict):
            return any(held(i) for i in item.values())
        if isinstance(item, list | tuple | set | frozenset):
            return any(held(i) for i in item)
        return False

    if isinstance(value, BaseModel):
        return any(held(getattr(value, name, None)) for name in type(value).model_fields)
    return False


def _whole(value: BaseModel, form: Any) -> Any:
    """`form` with the fields of each subclass instance `value` holds at an extra-allowing base,
    read as `value`'s own class reads it; `form` itself when there are none or the class would not
    read the result."""
    if not _holds_a_subclass_at_an_open_base(value):
        return form
    cls = type(value)
    try:
        whole = with_subclass_fields_at_open_bases(
            value, read_python_then_json(form, cls.model_validate, cls.model_validate_json), form
        )
        if whole is not form:
            read_python_then_json(whole, cls.model_validate, cls.model_validate_json)
    except Exception:
        return form
    return whole


def wire_models(value: Any, *, extra_forms: ExtraForms = "hierarchy") -> Any:
    """`value` with each model instance in it replaced by the JSON-ready form its class reads back.

    For values sent without a declared annotation (`call_rpc`, `call_async`,
    `call_rpc_no_wait`, so `RpcProxy`, and the payload of `publish_event` and
    `broadcast_message`), where the receiver's model is not known and the value's own class
    stands for it. `pydantic_core.to_jsonable_python` writes a model by
    alias, and a model the service reads by field name (`validate_by_alias=False`, a
    `serialization_alias` that differs from the `validation_alias`) is refused. Each model is
    written by alias first, as before, then by field name, then one model at a time
    (`nested_form`, with the validation-alias form where no model class of the hierarchy would
    read it as other values), and `choose_wire_form` sends the first its class reads back equal.
    A call that was accepted goes out as it did. When no model class of the hierarchy reads the
    chosen form faithfully and its own class would read a field as anything other than what its
    validators make of the caller's value, the call is refused before sending
    (`refuse_a_lost_value`).

    Dicts, lists, tuples and sets are walked; everything else is left for
    `to_jsonable_python`, so non-model values serialise as they always did.

    `extra_forms` is passed to `nested_form`, and says what each model level is offered beyond its
    dumps: `"none"` offers the dumps alone.
    """
    return _wire_models(value, extra_forms, "payload")


def _wire_models(value: Any, extra_forms: ExtraForms, path: str) -> Any:
    """`wire_models`, with `path` naming where `value` sits in what is sent."""
    if isinstance(value, BaseModel):
        try:
            first = pydantic_core.to_jsonable_python(value)
        except Exception as exc:
            # The dump itself fails: bytes that are not UTF-8, an object JSON cannot write, a
            # computed field that raises. Refused as a value that cannot go out, naming the field and
            # the model holding it, with the dump's own error as the cause and none of the value.
            where, holder = _where_the_dump_fails(value, path)
            raise RpcValidationError(
                details=[
                    {
                        "type": "value_cannot_be_written",
                        "loc": [where],
                        "msg": f"{holder} cannot be written as JSON ({type(exc).__name__})",
                    }
                ],
                message=(
                    f"refused before sending: {where} of {holder} cannot be written as JSON "
                    f"({type(exc).__name__})"
                ),
            ) from exc
        # A plain model has one form, its alias dump, so there is nothing to choose and nothing to
        # refuse, and it is sent without validating the dump back. What decides that is the class
        # test (no alias, validator or serializer), and for a datetime or time the walk's test of
        # its zone, which turns away the values the read-back refuses. The config allowlist and the
        # rest of the walk are defence against a later change in `refuse_a_lost_value` or
        # `misread_values`: no other value they turn away is sent differently today.
        if _is_plain_instance(value):
            return _whole(value, first)
        # The common case, decided by one validation: the class reads the alias form back as the
        # value. `_choose_among` would return it on the same check, and the refusal below would
        # pass on it, since the instance's own class leads `_model_classes`.
        if _reads_as_the_argument(type(value), first, value):
            return _whole(value, first)
        form = _choose_among(
            value,
            first,
            lambda: pydantic_core.to_jsonable_python(value, by_alias=False),
            lambda: nested_form(value, alias_first=True, extra_forms=extra_forms),
            first_refused=True,
        )
        if not any(_reads_faithfully(k, form, value) for k in _model_classes(value)):
            refuse_a_lost_value(type(value), form, value)
        return _whole(value, form)
    if isinstance(value, dict):
        return {
            key: _wire_models(item, extra_forms, f"{path}[{key!r}]") for key, item in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [
            _wire_models(item, extra_forms, f"{path}[{index}]") for index, item in enumerate(value)
        ]
    return value


def serialize_payload(data: Any, format: str = "json") -> tuple[bytes, str]:
    """Serialize data into bytes and return along with its Content-Type."""
    fmt = (format or "json").lower()
    if fmt == "msgpack":
        return pack_msgpack(data), CONTENT_TYPE_MSGPACK
    elif fmt == "json":
        json_data = pydantic_core.to_jsonable_python(data)
        return json.dumps(json_data).encode("utf-8"), CONTENT_TYPE_JSON
    else:
        raise ValueError(f"Unsupported serialization format: {format}")


def deserialize_payload(
    raw: bytes,
    content_type: str | None = None,
    fallback_format: str = "json",
) -> Any:
    """Deserialize payload bytes based on Content-Type header with graceful fallback."""
    if not raw:
        return {}
    ct = (content_type or "").lower().split(";")[0].strip()
    if ct == CONTENT_TYPE_MSGPACK or (not ct and fallback_format == "msgpack"):
        try:
            return unpack_msgpack(raw)
        except Exception as e:
            if not ct:
                # Fallback to json if untyped bytes fail msgpack
                try:
                    return json.loads(raw.decode("utf-8"))
                except Exception:
                    pass
            raise e
    else:
        try:
            return json.loads(raw.decode("utf-8"))
        except Exception as e:
            if not ct:
                # Fallback to msgpack if untyped bytes fail json
                if msgpack is not None:
                    try:
                        return unpack_msgpack(raw)
                    except Exception:
                        pass
            raise e


def validate_payload[M: BaseModel](model_type: type[M], payload: Any) -> M:
    """`payload` validated against `model_type`: the one step an RPC and an event share.

    An object, a `dict` or any other `Mapping`, is read without the `correlation_id` it carries,
    unless the model declares a field of that name: the id is the message's metadata, handed to a
    handler that asks for it separately, and a model built from a handler's parameters would
    otherwise refuse it as an extra field. Anything else is validated as it is, which a model built
    for a handler's parameters refuses.

    Raises pydantic's `ValidationError`. What a failure means is left to the caller: an RPC
    refuses and replies, an event follows its `on_invalid` policy.
    """
    if isinstance(payload, Mapping):
        payload = {
            key: value
            for key, value in payload.items()
            if not (key == "correlation_id" and "correlation_id" not in model_type.model_fields)
        }
    return validate_decoded(model_type, payload)


def _is_json_in_form(value: Any) -> bool:
    """Whether `value` holds only what JSON decodes to: objects with text keys, arrays, text,
    numbers, booleans and null. A foreign msgpack producer's `bytes`, a map with a `bytes` or an
    integer key, a timestamp, are python values that no JSON text carries."""
    if isinstance(value, dict):
        return all(isinstance(k, str) and _is_json_in_form(v) for k, v in value.items())
    if isinstance(value, list):
        return all(_is_json_in_form(v) for v in value)
    return value is None or isinstance(value, str | bool | int | float)


def read_python_then_json[T](
    payload: Any,
    in_python: Callable[[Any], T],
    in_json: Callable[[bytes], T],
) -> T:
    """`payload` read by a validator the way a service reads a message: python mode, then JSON mode.

    `in_python` and `in_json` are the two entry points of one validator (a model's `model_validate`
    and `model_validate_json`, or a `TypeAdapter`'s `validate_python` and `validate_json`). The
    service reads every payload through this, and the client reads what it is about to send through
    it, so the two cannot disagree about whether a form is accepted.

    Python mode first. Whatever it accepts is accepted exactly as it always was, so a lax model accepts
    what it did, with the same value, and a foreign msgpack producer's `bytes` or timestamp, which
    are python values, are read as they were.

    A body that is JSON in form is what a model declared strict refuses in python mode: an ISO
    string for a `datetime`, text for a `UUID` or a `Decimal`, an array for a tuple or a set. That
    is the form cliffracer's own senders write, over JSON and over msgpack alike (`pack_msgpack`
    dumps to JSON values first), so a strict model refused the dump of its own value. When python
    mode refuses a payload that is JSON in form, it is read again in JSON mode, pydantic's mode for
    JSON input, which accepts those forms and still refuses what a strict model should (`"1"` for an
    int, `1` for a bool). When both refuse, the JSON-mode error is the one raised: it names the
    violations that are real, and not the forms python mode refuses in a JSON body. For a lax model
    that error is worded as JSON mode words it, at the same location: "valid array" for a list,
    tuple, set or frozenset, "an object" for a dict or a model, "valid duration" for a timedelta. Its
    type is python mode's but for a whole number of magnitude 10**18 or more given for a date, a
    datetime or a timedelta, which is `*_type` where python mode said `*_parsing`.

    A payload with python values in it (`bytes`, a map with a `bytes` or an integer key) is not JSON
    in form: writing it as JSON would turn those into text and accept what a strict model refuses,
    or what could never have been sent as JSON. Its python-mode error is raised.

    One exception stays: a strict model with a `before` or `wrap` validator, on the model or on a
    field, refuses the JSON form of its own dump in JSON mode as well. That is pydantic's own
    `model_validate_json`, and it was refused before.
    """
    try:
        return in_python(payload)
    except PydanticValidationError as refused:
        if not _is_json_in_form(payload):
            raise
        try:
            encoded = pydantic_core.to_json(payload)
        except Exception:
            raise refused from None
        return in_json(encoded)


def validate_decoded[M: BaseModel](model_type: type[M], payload: Any) -> M:
    """`payload`, the decoded body of a message, validated against `model_type`.

    See `read_python_then_json` for what is accepted and what is raised.
    """
    return read_python_then_json(payload, model_type.model_validate, model_type.model_validate_json)
