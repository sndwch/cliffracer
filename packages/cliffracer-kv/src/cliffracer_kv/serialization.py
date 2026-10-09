"""Serialization and deserialization helpers for cliffracer-kv."""

from __future__ import annotations

import collections.abc
import dataclasses
import functools
import inspect
import io
import json
import math
import types
import typing
from collections.abc import Callable, Iterator
from typing import Any, cast

from pydantic import BaseModel, Secret, SecretBytes, SecretStr, TypeAdapter, ValidationError
from pydantic_core import to_jsonable_python

from cliffracer.core.validation import FormUnavailable, choose_wire_form, nested_form

from .errors import ModelDoesNotReadBackError


def serialize_value(value: Any) -> bytes:
    """Serialize a Python object to bytes for NATS KV storage.

    Supported formats:
    - Pydantic BaseModel -> its JSON, utf-8: under the field names, or under the aliases, whichever
      the model's class reads back as the model; when neither does, `ModelDoesNotReadBackError`
      (see `_model_json`)
    - bytes, bytearray -> raw bytes, a subclass of either included; inside a container, as JSON
      writes bytes. One that is also a dataclass with fields is refused (TypeError): its bytes and
      its fields are two values, and the write cannot know which is meant
    - str -> utf-8 bytes
    - anything else -> JSON, utf-8: dict, list, int, float, bool and None as they
      are, and, inside them or on their own, dataclasses (as objects), datetimes
      and dates (ISO 8601 strings), UUIDs and decimals (strings), enums (their values), and
      sets and tuples (arrays; a set's order is not defined). What pydantic writes as a
      string, such as a path or a timedelta, is stored as that string.

    A value that none of these covers raises TypeError, naming its type, instead
    of being stored as the text of its repr, which nothing could read back. A `SecretStr`, a
    `SecretBytes` or a generic `Secret[...]`, alone or held by a model, a dataclass, a dict (as a key
    or a value), a list or a set at any depth, raises
    TypeError naming where it is: a dump writes its mask, not its value, and a bucket is
    readable by every client with access to it. Call `get_secret_value()` to store the secret.
    So does a number JSON has no spelling for (NaN, infinity), and so does an iterator, a generator
    or a file object, which would be consumed to store its items: read it first.
    """
    _refuse_a_secret(value, type(value).__name__)
    if isinstance(value, BaseModel):
        return _model_json(value)
    if isinstance(value, bytes | bytearray):
        return bytes(value)
    if isinstance(value, str):
        return value.encode("utf-8")

    try:
        return json.dumps(value, default=_json_default, allow_nan=False).encode("utf-8")
    except (TypeError, ValueError) as err:
        raise TypeError(
            f"cannot store a {type(value).__name__} in a KV bucket or object store: it is "
            f"not a Pydantic model, bytes, str or JSON-serialisable ({err}). Convert it "
            "to one of those first."
        ) from err


# Exact builtin types that can neither be a secret nor hold one. Exact, not isinstance: a subclass
# of int may be a dataclass or anything else, so it is walked like any other object.
_NOTHING_INSIDE = frozenset({str, bytes, bytearray, int, float, bool, type(None)})

# Keys whose repr cannot fail or run code, so it is taken only when a refusal names the place. Not
# int: the repr of an int past `sys.get_int_max_str_digits()` raises.
_SPELLED_LATE = frozenset({str, bytes, float, bool, type(None)})

# Exact builtin containers: none is a secret, a model or a dataclass, so only its members are walked.
_PLAIN_CONTAINERS = frozenset({dict, list, tuple, set, frozenset})

# How `_place` spells one step of a place: `.name`, ` key`, `[repr(key)]`, or text spelled already.
_MEMBER, _KEY, _ITEM, _SPELLED = range(4)


def _refuse_a_secret(value: Any, where: Any, _seen: set[int] | None = None) -> None:
    """Raise TypeError naming the place of the first secret inside `value`.

    A secret is a `SecretStr`, a `SecretBytes` or a generic `Secret[...]`, wherever it sits: a value,
    a dict's key as well as its value.

    A model's fields are walked by value, so a secret held by a field typed `Any` is found as well
    as one typed as a secret, and so is one held as an extra of a model that allows them or returned
    by a computed field. A dataclass, standard or pydantic, is walked by its fields. A field declared
    `exclude=True` is not part of the dump and is not walked.

    `where` is the place: a string, or a `(place, how, step)` triple that `_place` spells out only
    when a secret is refused, since the walk visits every member of everything stored.
    """
    cls = type(value)
    if cls in _NOTHING_INSIDE:
        return
    plain = cls in _PLAIN_CONTAINERS
    if not plain and isinstance(value, SecretStr | SecretBytes | Secret):
        raise TypeError(
            f"cannot store a {type(value).__name__} in a KV bucket or object store "
            f"({_place(where)}): "
            "its dump is the mask '**********', not the secret, so the value would be lost, and "
            "a bucket is readable by every client with access to it. If you mean to store the "
            "secret, pass `get_secret_value()`."
        )
    if not plain and isinstance(value, bytes | bytearray) and dataclasses.is_dataclass(cls):
        if dataclasses.fields(cls):
            raise TypeError(
                f"cannot store a {type(value).__name__} in a KV bucket or object store "
                f"({_place(where)}): it is bytes and a dataclass with fields, two different values, "
                "and a write cannot know which is meant. Store its bytes (`bytes(value)`) or its "
                "fields (`dataclasses.asdict(value)`)."
            )
        return  # bytes with a type and nothing else: stored as plain bytes are
    if not plain and isinstance(value, bytes | bytearray | str):
        return
    seen = _seen if _seen is not None else set()
    if id(value) in seen:
        return
    if not plain and isinstance(value, BaseModel):
        seen.add(id(value))
        for name, field in type(value).model_fields.items():
            if not field.exclude:
                _refuse_a_secret(getattr(value, name, None), (where, _MEMBER, name), seen)
        for name, item in (value.model_extra or {}).items():
            # An extra set by hand after validation may be named by any object.
            step = (_MEMBER, name) if type(name) is str else (_SPELLED, f".{name}")
            _refuse_a_secret(item, (where, *step), seen)
        for name in type(value).model_computed_fields:
            _refuse_a_secret(getattr(value, name, None), (where, _MEMBER, name), seen)
    elif not plain and dataclasses.is_dataclass(value):
        seen.add(id(value))
        excluded = {
            name for name, info in getattr(value, "__pydantic_fields__", {}).items() if info.exclude
        }
        for member in dataclasses.fields(value):
            if member.name not in excluded:
                _refuse_a_secret(
                    getattr(value, member.name, None), (where, _MEMBER, member.name), seen
                )
    elif isinstance(value, dict):
        seen.add(id(value))
        for key, item in value.items():
            _refuse_a_secret(key, (where, _KEY, None), seen)
            # Any other key's repr is taken here, as spelling every place up front took it: it may
            # raise or run code, and does so whether or not anything is refused.
            if type(key) in _SPELLED_LATE:
                _refuse_a_secret(item, (where, _ITEM, key), seen)
            else:
                _refuse_a_secret(item, (where, _SPELLED, f"[{key!r}]"), seen)
    elif isinstance(value, list | tuple | set | frozenset):
        seen.add(id(value))
        for index, item in enumerate(value):
            _refuse_a_secret(item, (where, _ITEM, index), seen)


def _place(where: Any) -> str:
    """The place a `_refuse_a_secret` walk names, such as `Order.lines[0]['sku']`."""
    steps = []
    while type(where) is tuple:
        where, how, step = where
        if how == _MEMBER:
            steps.append(f".{step}")
        elif how == _KEY:
            steps.append(" key")
        elif how == _ITEM:
            steps.append(f"[{step!r}]")
        else:
            steps.append(step)
    return cast(str, where) + "".join(reversed(steps))


def _json_default(value: Any) -> Any:
    """What `json.dumps` stores for a value it cannot write itself.

    Pydantic knows how to write an iterator as an array, which reads it to the end: a file
    object stored that way is stored as its lines and left at the end of the file. A value that
    is read by being consumed is refused, wherever it sits in what is being stored.
    """
    if isinstance(value, bytes | bytearray):
        # Bytes with a type of their own (a fieldless dataclass): written as plain bytes are, not as
        # an empty object.
        return to_jsonable_python(bytes(value))
    if isinstance(value, Iterator | io.IOBase):
        raise TypeError(
            f"a {type(value).__name__} is read by being consumed, so storing it would store its "
            "items and leave it used up; read it first"
        )
    return to_jsonable_python(value)


def _model_json(value: BaseModel) -> bytes:
    """A model's JSON in a form its own class reads back as the model: what `get(as_type=...)` returns.

    A reader may be the model's own class or any base class that declares some of its fields, so a
    form is chosen in three steps:

    1. The field names, then the aliases, then the model written one model at a time with each
       field where its validation alias reads it (`nested_form`, the form the RPC client offers
       third): the first form that the model's own class and every base class declaring fields
       read back as the model (`choose_wire_form`, as the RPC client chooses a form). A model with
       no such base, which is most, takes its own class's choice, and one that read back under its
       field names keeps those bytes. A model read only through `AliasChoices` or `AliasPath`, or a
       tree whose levels read different forms, is stored in the third. The third is built only when
       neither dump serves (`_validation_alias_form`), and is not offered when it would hold a NaN
       or an infinity the model does not write as a JSON constant.
    2. When no form serves every class, the form earlier releases stored (`_released_form`), if the
       model's own class reads it back as the model: every reader then reads what it read before,
       so no base class reads a value it did not read before.
    3. Else a form the model's own class reads back as the model and no base class reads worse than
       it read the released form: a base may fail to read it only where it failed to read the
       released form too, and none may read another value silently.

    When none of these exists, `ModelDoesNotReadBackError` names the model and what each form read
    back as.

    "Reads back as the model" is judged by declared type (`_stores_the_same`, `_same_at`): a value at
    a typed position must read back as the value it held, NaN equal to NaN; a value at a position
    declared `Any` or `object`, at any depth (a field, a list or tuple item, a dict value, a union
    arm; through a type alias or a `TypeVar` too, see `_declared`), and every extra, must read back with the same JSON form, which is all such a position
    promises. A private attribute and a field declared `exclude=True` are not stored, so what they
    hold is not compared. A base class is judged on the fields it declares, against what it makes
    of the model's values.
    """
    model = type(value)
    forms = {
        "field names": value.model_dump_json(),
        "aliases": value.model_dump_json(by_alias=True),
    }
    bases = _declaring_bases(model)

    def every_class_reads(text: str) -> bool:
        return _reads_back_equal(model, text, value) and all(
            _base_reads(base, text, value) is True for base in bases
        )

    def read(text: str) -> Any:
        # The model itself when every class reads the form back as it, and otherwise what its own
        # class read, which `choose_wire_form` below never takes for the model.
        back = model.model_validate_json(text)
        return value if every_class_reads(text) else back

    def offer(text: str) -> Callable[[], str]:
        return lambda: text

    # Taken for the model only when `read` handed back the model itself, which it does exactly
    # for a form every class reads: a form only the model's own class reads back as equal (a base
    # that reads by alias alone, say) is not chosen ahead of one every class reads.
    chosen: str = choose_wire_form(
        value, [offer(text) for text in forms.values()], read, alike=lambda b, v: b is v
    )
    if every_class_reads(chosen):
        return chosen.encode("utf-8")
    # The third form is offered last, so it is built only when neither dump serves every class.
    third = _validation_alias_form(value, forms)
    if third is not None:
        forms["validation aliases"] = third
        if every_class_reads(third):
            return third.encode("utf-8")
    released = _released_form(value, forms)
    if _reads_back_equal(model, released, value):
        return released.encode("utf-8")
    for text in forms.values():
        if _reads_back_equal(model, text, value) and all(
            _no_worse_than_released(base, text, released, value) for base in bases
        ):
            return text.encode("utf-8")
    raise ModelDoesNotReadBackError(
        f"cannot store a {model.__name__} in a KV bucket or object store: no form it can be "
        "written in reads back as the same model through its own class and each base class that "
        "declares its fields, so a read would return other values than were written. "
        + "; ".join(
            f"under its {label} {text} {_what_it_reads_as(model, text)}"
            + "".join(
                f", while {base.__name__} {_what_it_reads_as(base, text)}"
                for base in bases
                if _reads_back_equal(model, text, value)
                and not _no_worse_than_released(base, text, released, value)
            )
            for label, text in forms.items()
        )
        + ". Give the fields aliases that the model both writes and reads, or store a dict."
    )


def _validation_alias_form(value: BaseModel, dumps: dict[str, str]) -> str | None:
    """`value` written one model at a time with each field where its validation alias reads it
    (`nested_form`), as JSON, or None where there is no such form: `nested_form` cannot write it,
    it equals one of `dumps`, or it holds a NaN or an infinity that the model's own dump writes as
    `null`. JSON has no number for either, and the literal is written only where Pydantic's own
    dump writes one (`_written_as_the_models_dump_writes_it`)."""
    try:
        written = nested_form(value, alias_first=False, extra_forms="caller checks")
    except FormUnavailable:
        return None
    if any(written == json.loads(text) for text in dumps.values()):
        return None
    constants = _written_as_the_models_dump_writes_it(value, written)
    try:
        return json.dumps(written, ensure_ascii=False, separators=(",", ":"), allow_nan=constants)
    except ValueError:
        return None


def _written_as_the_models_dump_writes_it(value: BaseModel, written: Any) -> bool:
    """Whether every NaN and infinity in `written` is one the model's own `model_dump_json` writes
    as a JSON constant: Pydantic writes as many NaN and Infinity constants as `written` holds
    non-finite numbers.

    Pydantic decides where it writes a literal, and not always by the config of the model holding
    the number: a model in an `OrderedDict`, a `Sequence` or a union of models, and an extra, are
    written under the outermost model's config. So it is asked rather than followed. `written` is
    made through the same serializers, so a serializer that writes a NaN is counted on both sides,
    and a NaN the dump writes as `null` leaves `written` with one more than the dump."""
    count = _non_finite_count(written)
    if not count:
        return False
    constants: list[str] = []

    def note(constant: str) -> float:
        constants.append(constant)
        return 0.0

    json.loads(value.model_dump_json(), parse_constant=note)
    return len(constants) == count


def _non_finite_count(written: Any) -> int:
    """How many NaNs and infinities a JSON-ready value (dicts, lists and scalars) holds."""
    if isinstance(written, float):
        return 0 if math.isfinite(written) else 1
    if isinstance(written, dict):
        return sum(_non_finite_count(item) for item in written.values())
    if isinstance(written, list):
        return sum(_non_finite_count(item) for item in written)
    return 0


def _declaring_bases(model: type[BaseModel]) -> list[type[BaseModel]]:
    """The base classes of `model` that declare fields: each could be the type a reader asks for."""
    return [
        base
        for base in model.__mro__[1:]
        if isinstance(base, type)
        and issubclass(base, BaseModel)
        and base is not BaseModel
        and base.model_fields
    ]


def _released_form(value: BaseModel, forms: dict[str, str]) -> str:
    """The form earlier releases stored: the field names when the model's class validated them,
    else the aliases when it validated those, else the field names."""
    model = type(value)
    for text in (forms["field names"], forms["aliases"]):
        try:
            model.model_validate_json(text)
        except Exception:
            continue
        return text
    return forms["field names"]


def _base_reads(base: type[BaseModel], text: str, value: BaseModel) -> bool | None:
    """Whether `base` reads `text` back as it makes of `value`, on the fields it declares: True when
    it does, False when it reads another value, None when it cannot read `text` at all."""
    try:
        back = base.model_validate_json(text)
    except Exception:
        return None
    data = {name: getattr(value, name) for name in base.model_fields if hasattr(value, name)}
    try:
        # By field name only: the keys are field names, and a field may alias another's name.
        expected: Any = base.model_validate(data, by_alias=False, by_name=True)
    except Exception:
        expected = value
    for name, field in base.model_fields.items():
        if field.exclude:
            continue
        if not _same_at(getattr(back, name), getattr(expected, name, None), field.annotation):
            return False
    return True


def _no_worse_than_released(
    base: type[BaseModel], text: str, released: str, value: BaseModel
) -> bool:
    """`base` reads `text` back as the model, or cannot read it where it could not read the
    released form back as the model either. Reading another value silently never passes."""
    now = _base_reads(base, text, value)
    if now is True:
        return True
    if now is False:
        return False
    return _base_reads(base, released, value) is not True


def _reads_back_equal(model: type[BaseModel], text: str, value: BaseModel) -> bool:
    try:
        return _stores_the_same(model.model_validate_json(text), value)
    except Exception:
        return False


def _stores_the_same(read: Any, written: Any) -> bool:
    """Whether `read` holds what `written` holds where a dump writes it, judged by declared type.

    A model compares each field a dump writes (not one declared `exclude=True`, and no private
    attribute) at the type the field declares (`_same_at`), and every extra by its JSON form. A
    dataclass compares its fields the same way; a dict, list or tuple item by item; a float NaN
    equals NaN. Anything else compares with `==`, and a comparison that raises is "not the same".
    """
    if isinstance(read, BaseModel) or isinstance(written, BaseModel):
        if type(read) is not type(written):
            return False
        for name, field in type(written).model_fields.items():
            if field.exclude:
                continue
            if not _same_at(getattr(read, name), getattr(written, name), field.annotation):
                return False
        return _same_json(read.model_extra or {}, written.model_extra or {})
    if dataclasses.is_dataclass(written) and not isinstance(written, type):
        if type(read) is not type(written):
            return False
        try:
            hints = typing.get_type_hints(type(written))
        except Exception:
            hints = {}
        return all(
            _same_at(
                getattr(read, field.name),
                getattr(written, field.name),
                hints.get(field.name, _UNDECLARED),
            )
            for field in dataclasses.fields(written)
        )
    if isinstance(read, float) and isinstance(written, float):
        return read == written or (math.isnan(read) and math.isnan(written))
    if isinstance(read, dict) and isinstance(written, dict):
        return read.keys() == written.keys() and all(
            _stores_the_same(read[key], written[key]) for key in written
        )
    if isinstance(read, list | tuple) and type(read) is type(written):
        return len(read) == len(written) and all(
            _stores_the_same(a, b) for a, b in zip(read, written, strict=True)
        )
    try:
        return bool(read == written)
    except Exception:
        return False


#: A position whose type is not declared (a dataclass field whose annotation cannot be resolved):
#: compared exactly, as a typed position is.
_UNDECLARED = object()

#: Types that promise no more than a JSON value at their position.
_UNTYPED: tuple[Any, ...] = (Any, object)

#: The bare containers, whose items are `Any`.
_BARE: tuple[type, ...] = (dict, list, tuple)


def _declared(annotation: Any) -> Any:
    """`annotation` as the type it declares: `Annotated` metadata dropped, a type alias replaced by
    its value (a generic one with its arguments), and a `TypeVar` by its bound, the union of its
    constraints, or `Any` when it has neither, which is how a model validates it."""
    while True:
        origin = typing.get_origin(annotation)
        if origin is typing.Annotated:
            annotation = typing.get_args(annotation)[0]
        elif _is_type_alias(annotation):
            annotation = annotation.__value__
        elif _is_type_alias(origin):
            try:
                annotation = origin.__value__[typing.get_args(annotation)]
            except TypeError:
                annotation = origin.__value__
        elif isinstance(annotation, typing.TypeVar):
            if annotation.__bound__ is not None:
                annotation = annotation.__bound__
            elif annotation.__constraints__:
                return typing.Union[annotation.__constraints__]  # noqa: UP007
            else:
                return Any
        else:
            return annotation


def _is_type_alias(annotation: Any) -> bool:
    """`type X = ...`, or `typing_extensions.TypeAliasType("X", ...)`, which is another class."""
    return type(annotation).__name__ == "TypeAliasType" and hasattr(annotation, "__value__")


def _same_at(read: Any, written: Any, annotation: Any) -> bool:
    """Whether `read` holds what `written` holds at a position declared `annotation`.

    Where the declared type is `Any` or `object`, at any depth (a list or tuple item, a dict value,
    a union arm, a bare `dict`, `list` or `tuple`, whose items are `Any`), the value promised no
    more than its JSON, so only its JSON form is compared. Everywhere else the value itself is. A
    type alias, an `Annotated` type and a `TypeVar` are judged as the type they declare
    (`_declared`). A value in a union is judged under every arm it is an instance of (`_holds`),
    so by its JSON form only where each of those arms promises no more. A typed set or frozenset
    matches its members one to one under the item type (`_same_members`), not by hash.
    """
    annotation = _declared(annotation)
    if annotation in _UNTYPED or annotation in _BARE:
        return _same_json(read, written)
    origin = typing.get_origin(annotation)
    args = typing.get_args(annotation)
    if origin in (typing.Union, types.UnionType):
        if read is None or written is None:
            return read is None and written is None
        arms = [_declared(arm) for arm in args if arm is not type(None)]
        if len(arms) == 1:
            return _same_at(read, written, arms[0])
        # Judged under every arm the written value is, so a value a typed arm holds is compared by
        # value even where another arm promises only its JSON, and by JSON form only where every
        # arm it is promises no more. A value no arm holds strictly is compared exactly.
        holding = [arm for arm in arms if _holds(arm, written)]
        if not holding:
            return _stores_the_same(read, written)
        return all(_same_at(read, written, arm) for arm in holding)
    if origin in (dict, collections.abc.Mapping, collections.abc.MutableMapping) and args:
        if not (isinstance(read, dict) and isinstance(written, dict)):
            return _stores_the_same(read, written)
        return read.keys() == written.keys() and all(
            _same_at(read[key], written[key], args[1]) for key in written
        )
    if origin in (list, collections.abc.Sequence, collections.abc.MutableSequence) and args:
        if not (isinstance(read, list) and isinstance(written, list)):
            return _stores_the_same(read, written)
        return len(read) == len(written) and all(
            _same_at(a, b, args[0]) for a, b in zip(read, written, strict=True)
        )
    if origin is tuple and args:
        if not (isinstance(read, tuple) and isinstance(written, tuple)):
            return _stores_the_same(read, written)
        if len(read) != len(written):
            return False
        types_at = [args[0]] * len(written) if args[-1] is Ellipsis else list(args)
        return len(types_at) == len(written) and all(
            _same_at(a, b, item_type)
            for a, b, item_type in zip(read, written, types_at, strict=True)
        )
    if origin in (set, frozenset) and args and args[0] in _UNTYPED:
        return _same_json(read, written)
    if origin in (set, frozenset, collections.abc.Set, collections.abc.MutableSet) and args:
        if not (isinstance(read, set | frozenset) and isinstance(written, set | frozenset)):
            return _stores_the_same(read, written)
        return _same_members(read, written, args[0])
    return _stores_the_same(read, written)


def _same_members(
    read: set[Any] | frozenset[Any], written: set[Any] | frozenset[Any], item: Any
) -> bool:
    """Whether each member read back matches a member written, one to one, under `item`.

    A set finds its members by hash, and a NaN hashes by identity, so a NaN read back (or a frozen
    model holding one) is never found in the set written. Members are matched by value instead,
    as a list's items are, NaN equal to NaN.
    """
    if len(read) != len(written):
        return False
    unmatched = list(written)
    for member in read:
        for index, candidate in enumerate(unmatched):
            if _same_at(member, candidate, item):
                del unmatched[index]
                break
        else:
            return False
    return True


@functools.lru_cache(maxsize=256)
def _adapter(annotation: Any) -> TypeAdapter[Any]:
    return TypeAdapter(annotation)


def _holds(annotation: Any, value: Any) -> bool:
    """Whether `value` is an `annotation` as it stands, by strict validation. A type pydantic cannot
    check holds the value when the value is an instance of its class."""
    if annotation in _UNTYPED:
        return True
    try:
        hash(annotation)
    except TypeError:
        make: Callable[[Any], TypeAdapter[Any]] = TypeAdapter
    else:
        make = _adapter
    try:
        adapter = make(annotation)
    except Exception:
        # No schema for it (an arbitrary class without `arbitrary_types_allowed`), or another
        # failure building one: it holds what is an instance of its class, or of its origin when
        # it is parameterised, and a type that is not a class holds anything.
        kind = typing.get_origin(annotation) or annotation
        return not isinstance(kind, type) or isinstance(value, kind)
    try:
        adapter.validate_python(value, strict=True)
    except ValidationError:
        return False
    except Exception:
        return True
    return True


def _same_json(read: Any, written: Any) -> bool:
    """Whether `read` and `written` have the same JSON form, NaN equal to NaN."""
    try:
        return _stores_the_same(to_jsonable_python(read), to_jsonable_python(written))
    except Exception:
        return False


def _what_it_reads_as(model: type[BaseModel], text: str) -> str:
    try:
        return f"reads as {model.model_validate_json(text)!r}"
    except ValidationError as refused:
        return f"is refused ({refused.error_count()} error(s), first: {refused.errors()[0]['msg']})"
    except Exception as refused:
        return f"is refused ({type(refused).__name__})"


def deserialize_value[T](
    data: bytes | None,
    as_type: type[T] | None = None,
    default: Any = None,
) -> T | Any:
    """Deserialize raw bytes from NATS KV storage into a target type or inferred structure.

    - If data is None, returns default.
    - If as_type is a Pydantic BaseModel subclass, uses model_validate_json.
    - If as_type is bytes, returns data directly.
    - If as_type is str, decodes data as utf-8.
    - If as_type in (dict, list), parses as JSON and raises ValueError if the result is not one.
    - If as_type is callable, passes it the JSON-parsed value, and if that raises, the decoded text.
    - If as_type is None, parses JSON if valid, else returns decoded utf-8 string,
      or raw bytes if non-decodable. A str that is valid JSON ("123", "true", "null") therefore
      comes back as the JSON value, and bytes that decode come back as str; as_type=str and
      as_type=bytes return exactly what was stored.
    """
    if data is None:
        return default

    if as_type is not None:
        if inspect.isclass(as_type) and issubclass(as_type, BaseModel):
            return as_type.model_validate_json(data)
        if as_type is bytes:
            return data
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            if as_type is str:
                return data.decode("utf-8", errors="replace")
            raise
        if as_type is str:
            return text
        if as_type in (dict, list):
            parsed = json.loads(text)
            if not isinstance(parsed, cast(type, as_type)):
                raise ValueError(
                    f"the stored value is of type {type(parsed).__name__}, not {as_type.__name__}"
                )
            return parsed
        if callable(as_type):
            as_fn = cast(Callable[..., Any], as_type)
            try:
                parsed = json.loads(text)
                return as_fn(parsed)
            except Exception:
                return as_fn(text)

    # Inferred deserialization
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError:
        return data

    try:
        return json.loads(text)
    except (json.JSONDecodeError, ValueError):
        return text
