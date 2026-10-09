"""A service with a strict model accepts the JSON form of the model's own dump.

A payload is decoded and then validated in pydantic's python mode. A model declared strict, or a
field declared strict, refuses the forms JSON has to use there: an ISO string for a `datetime`, text
for a `UUID` or a `Decimal`, an array for a tuple or a set. So a service refused `model_dump(mode=
"json")` of its own value, from whichever client sent it, over JSON and over msgpack alike (cliffracer
dumps to JSON values before it packs msgpack).

`validate_decoded` validates in python mode first and, only when that refuses, once more in JSON mode.
What python mode accepts is accepted exactly as before, so a lax model accepts what it did and a
foreign msgpack producer's python values (`bytes`, timestamps) are read as they were. What it refuses
and JSON mode accepts is the JSON form a strict model could never receive. When both refuse, the
JSON-mode error is raised, since it names the real violations; when the payload cannot be written as
JSON, the python-mode error is.
"""

import datetime
import decimal
import random
import uuid

import msgpack
import pydantic_core
import pytest
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, ValidationError, field_validator

from cliffracer import ServiceConfig
from cliffracer.core.validation import (
    CONTENT_TYPE_JSON,
    CONTENT_TYPE_MSGPACK,
    deserialize_payload,
    pack_msgpack,
    validate_decoded,
    validate_payload,
)
from cliffracer.testing.messages import MockMessage
from tests.fixtures.strict_payloads import DUMP, RECORD, WHEN, Blob, Record, Records, Stamped

pytestmark = pytest.mark.unit


class Loose(BaseModel):
    """Lax, with a union whose member the JSON form does not name."""

    when: datetime.date | datetime.datetime


@pytest.fixture
async def service():
    Records.received = []
    service = Records(ServiceConfig(name="records", health_port=0))
    service._discover_handlers()
    await service.container._setup_extensions()
    yield service
    await service.container._stop_extensions()


async def _call(service, method, body: bytes, content_type: str):
    msg = MockMessage(
        f"records.rpc.{method}", body, headers={"Content-Type": content_type}, reply="_INBOX.r"
    )
    await service.container._handle_rpc_request(msg)
    return deserialize_payload(
        msg.responded_data, content_type=msg.response_headers["Content-Type"]
    )


async def _publish(service, subject, body: bytes, content_type: str):
    message = MockMessage(subject, body, headers={"Content-Type": content_type}, reply=None)
    return await service.container._dispatch_event(message, pattern=subject)


def _json(value) -> tuple[bytes, str]:
    return pydantic_core.to_json(value), CONTENT_TYPE_JSON


def _msgpack(value) -> tuple[bytes, str]:
    return pack_msgpack(value), CONTENT_TYPE_MSGPACK


# --- a service takes the JSON form of its own dump, over both formats ---------------------------------


@pytest.mark.parametrize("encode", [_json, _msgpack], ids=["json", "msgpack"])
async def test_an_rpc_with_a_strict_model_accepts_the_json_form_of_its_own_dump(service, encode):
    body, content_type = encode({"record": DUMP})

    reply = await _call(service, "put", body, content_type)

    assert reply["success"] is True, reply
    assert reply["result"] == str(RECORD.ident)
    assert service.received == [RECORD]


@pytest.mark.parametrize("encode", [_json, _msgpack], ids=["json", "msgpack"])
async def test_an_event_listener_with_a_strict_model_accepts_the_json_form_of_its_own_dump(
    service, encode
):
    body, content_type = encode(DUMP)

    await _publish(service, "records.put", body, content_type)

    assert service.received == [RECORD]


@pytest.mark.parametrize("encode", [_json, _msgpack], ids=["json", "msgpack"])
async def test_a_typed_listener_given_one_value_that_is_not_an_object_accepts_its_json_form(
    service, encode
):
    """The path that wraps a lone value as the handler's one parameter and validates it directly."""
    body, content_type = encode([DUMP])

    await _publish(service, "records.batch", body, content_type)

    assert service.received == [[RECORD]]  # each parameter as the type it declares


# --- a foreign msgpack producer's python values are read as before ------------------------------------


async def test_a_foreign_msgpack_producers_bytes_are_accepted_by_the_first_attempt(service):
    body = msgpack.packb({"blob": {"raw": b"\xff\x00"}}, use_bin_type=True)

    reply = await _call(service, "blob", body, CONTENT_TYPE_MSGPACK)

    assert reply["success"] is True and reply["result"] == 2, reply
    assert service.received == [Blob(raw=b"\xff\x00")]


# --- what a strict model should refuse is still refused -------------------------------------------------


@pytest.mark.parametrize("encode", [_json, _msgpack], ids=["json", "msgpack"])
async def test_an_rpc_still_refuses_what_a_strict_model_should(service, encode):
    refused = {**DUMP, "count": "2"}  # text for an int
    body, content_type = encode({"record": refused})

    reply = await _call(service, "put", body, content_type)

    assert reply["success"] is False and service.received == []
    assert [tuple(d["loc"]) for d in reply["details"]] == [("record", "count")]


def test_each_form_a_strict_model_refuses_is_refused_in_json_form_too():
    for field, value in (("count", "2"), ("count", 2.5), ("count", True), ("when", 1700000000)):
        with pytest.raises(ValidationError) as caught:
            validate_decoded(Record, {**DUMP, field: value})

        assert [e["loc"] for e in caught.value.errors()] == [(field,)], (field, value)


# --- the error is the real one ------------------------------------------------------------------------


def test_when_both_modes_refuse_the_error_names_only_what_is_really_wrong():
    payload = {**DUMP, "ident": "not-a-uuid"}

    with pytest.raises(ValidationError) as in_python:
        Record.model_validate(payload)
    with pytest.raises(ValidationError) as caught:
        validate_decoded(Record, payload)

    assert {e["loc"] for e in in_python.value.errors()} > {
        ("ident",)
    }  # python mode also lists the forms
    assert [e["loc"] for e in caught.value.errors()] == [("ident",)]


def test_a_payload_with_python_values_in_it_is_refused_with_the_python_mode_error():
    """Written as JSON, `bytes` would be text and `raw` would pass: it was never JSON in form."""
    payload = {"raw": b"\xff", "when": "2026-01-02T03:04:05"}  # `when` is a refusal, `raw` is not

    with pytest.raises(ValidationError) as in_python:
        Stamped.model_validate(payload)
    with pytest.raises(ValidationError) as caught:
        validate_decoded(Stamped, payload)

    assert caught.value.errors() == in_python.value.errors()
    assert [e["loc"] for e in caught.value.errors()] == [("when",)]


def test_a_strict_str_is_not_given_bytes_by_the_second_attempt():
    class Named(BaseModel):
        model_config = ConfigDict(strict=True)

        name: str

    with pytest.raises(ValidationError):
        validate_decoded(Named, {"name": b"x"})  # a foreign producer's bin is not text
    assert validate_decoded(Named, {"name": "x"}) == Named(name="x")


def test_python_values_deep_in_arrays_and_objects_are_found():
    class Names(BaseModel):
        model_config = ConfigDict(strict=True)

        names: list[str]
        nested: dict[str, list[str]] = {}

    with pytest.raises(ValidationError):
        validate_decoded(Names, {"names": ["a", b"x"]})
    with pytest.raises(ValidationError):
        validate_decoded(Names, {"names": [], "nested": {"k": [b"x"]}})
    assert validate_decoded(Names, {"names": ["a"], "nested": {"k": ["b"]}}).names == ["a"]


@pytest.mark.parametrize("key", [b"seq", 1, None, 2.5], ids=repr)
def test_a_map_with_a_key_that_is_not_text_is_refused_as_it_was(key):
    """The field is read from the key's JSON text (`"1"` for `1`), so that map written as JSON is
    accepted: it is refused because it is not JSON in form, and is never re-read."""
    (text,) = pydantic_core.from_json(pydantic_core.to_json({key: 0}))

    class Seq(BaseModel):
        seq: int = Field(alias=text)

    with pytest.raises(ValidationError) as caught:
        validate_decoded(Seq, {key: 1})

    assert [e["type"] for e in caught.value.errors()] == ["missing"]


def test_a_payload_that_is_json_in_form_but_cannot_be_written_as_json_keeps_the_python_error():
    """A lone surrogate decodes from JSON text and cannot be encoded back."""
    payload = {**DUMP, "ident": "\ud800"}

    with pytest.raises(ValidationError) as in_python:
        Record.model_validate(payload)
    with pytest.raises(ValidationError) as caught:
        validate_decoded(Record, payload)

    assert caught.value.errors() == in_python.value.errors()


# --- nothing python mode accepts changes --------------------------------------------------------------


def test_a_lax_model_gets_the_value_python_mode_gives_it():
    """JSON mode would read a datetime at midnight as the `datetime` the service held."""
    payload = {"when": "2026-01-01T00:00:00"}

    assert Loose.model_validate(payload).when == datetime.date(2026, 1, 1)
    assert validate_decoded(Loose, payload).when == datetime.date(2026, 1, 1)
    assert Loose.model_validate_json(pydantic_core.to_json(payload)).when == datetime.datetime(
        2026, 1, 1
    )


def test_the_shared_step_applies_it_after_removing_the_correlation_id():
    class Forbidding(BaseModel):
        model_config = ConfigDict(strict=True, extra="forbid")

        when: datetime.datetime

    model = validate_payload(Forbidding, {"when": "2026-01-02T03:04:05", "correlation_id": "c-1"})

    assert model.when == WHEN


# --- a seeded differential over random models --------------------------------------------------------


class Inner(BaseModel):
    n: int


def _random_model(rng: random.Random, index: int) -> tuple[type[BaseModel], dict, bool]:
    scalars = {
        "int": (int, lambda: rng.randint(-5, 50)),
        "str": (str, lambda: rng.choice(["a", "b c", ""])),
        "float": (float, lambda: rng.choice([0.5, 2.25])),
        "bool": (bool, lambda: rng.choice([True, False])),
        "datetime": (datetime.datetime, lambda: WHEN),
        "uuid": (uuid.UUID, lambda: uuid.UUID(int=rng.randint(1, 9))),
        "decimal": (decimal.Decimal, lambda: decimal.Decimal(rng.choice(["1.5", "10"]))),
        "tags": (set[int], lambda: {rng.randint(1, 3)}),
        "pair": (tuple[int, str], lambda: (rng.randint(0, 3), "t")),
        "items": (list[int], lambda: [rng.randint(0, 3)]),
        "frozen": (frozenset[int], lambda: frozenset({rng.randint(1, 3)})),
        "mapping": (dict[str, int], lambda: {"k": rng.randint(0, 3)}),
        "inner": (Inner, lambda: Inner(n=rng.randint(0, 3))),
        "day": (datetime.date, lambda: datetime.date(2026, 1, rng.randint(1, 9))),
        "span": (datetime.timedelta, lambda: datetime.timedelta(seconds=rng.randint(1, 90))),
        "either": (
            datetime.date | datetime.datetime,
            lambda: rng.choice([datetime.date(2026, 1, 1), WHEN]),
        ),
    }
    names = [f"f{i}" for i in range(rng.randint(1, 4))]
    annotations, values, namespace = {}, {}, {}
    strict = False
    for name in names:
        kind = rng.choice(list(scalars))
        annotation, make = scalars[kind]
        if kind != "either" and rng.random() < 0.2:
            namespace[name] = Field(strict=True)
            strict = True
        annotations[name], values[name] = annotation, make()
    namespace["__annotations__"] = annotations
    if rng.random() < 0.4:
        namespace["model_config"] = ConfigDict(strict=True)
        strict = True
    model = type(f"Random{index}", (BaseModel,), namespace)
    return (
        model,
        TypeAdapter(model).dump_python(model.model_construct(**values), mode="json"),
        strict,
    )


def _mutations(payload: dict):
    yield payload
    for key in payload:
        yield {**payload, key: b"x"}  # a foreign producer's bin
    yield {b"f0": 1, **payload}  # a map with a bytes key
    for key in payload:
        for value in ("x", 7, 1.5, None, True, "1", "2026-01-01", [], {}, 10**20, -(10**20)):
            yield {**payload, key: value}
        yield {k: v for k, v in payload.items() if k != key}
    yield {**payload, "extra": 1}


def _outcome(call, *args):
    try:
        return ("ok", call(*args))
    except ValidationError as error:
        errors = error.errors()
        return ("refused", [(e["loc"], e["type"]) for e in errors], [e["msg"] for e in errors])


# What a lax model's refusal says differently now, since the error is JSON mode's: (what python mode
# said, what JSON mode says), by how each message starts.
REWORDED = {
    "list": ("Input should be a valid list", "Input should be a valid array"),
    "tuple": ("Input should be a valid tuple", "Input should be a valid array"),
    "set": ("Input should be a valid set", "Input should be a valid array"),
    "frozenset": ("Input should be a valid frozenset", "Input should be a valid array"),
    "dict or model": ("Input should be a valid dictionary", "Input should be an object"),
    "timedelta": ("Input should be a valid timedelta", "Input should be a valid duration"),
}

# The one change of error type: a whole number of magnitude 10**18 or more in a date, a datetime or
# a timedelta, which python mode reads as a timestamp it cannot parse and JSON mode as not one.
RETYPED = {
    ("date_from_datetime_parsing", "date_type"),
    ("datetime_parsing", "datetime_type"),
    ("time_delta_parsing", "time_delta_type"),
}
BIG = 10**18


def _as_json(model, payload):
    return model.model_validate_json(pydantic_core.to_json(payload))


def _has_python_values(value) -> bool:
    if isinstance(value, dict):
        return any(not isinstance(k, str) or _has_python_values(v) for k, v in value.items())
    if isinstance(value, list):
        return any(_has_python_values(v) for v in value)
    return isinstance(value, bytes)


def test_over_random_models_nothing_python_mode_accepts_changes_and_strict_models_take_json():
    """Compares acceptance, the value accepted, and for a refusal the type, the location AND the message."""
    rng = random.Random(20261003)
    reworded = dict.fromkeys(REWORDED, 0)
    counts = {
        "accepted before": 0,
        "strict taken": 0,
        "strict still refused": 0,
        "lax refused, same error": 0,
        "lax refused, retyped": 0,
        "python values, refused as before": 0,
    }
    for index in range(80):
        model, dump, strict = _random_model(rng, index)
        for payload in _mutations(dump):
            where = (model.__name__, payload)
            today = _outcome(model.model_validate, payload)
            now = _outcome(validate_decoded, model, payload)
            as_json = _outcome(_as_json, model, payload)
            if today[0] == "ok":
                assert now == today, where  # accepted: identical, the value included
                counts["accepted before"] += 1
            elif _has_python_values(payload):
                assert now == today, (
                    where
                )  # not JSON in form: refused with the same error, message too
                counts["python values, refused as before"] += 1
            elif as_json[0] == "ok":
                assert now == as_json, where
                assert strict, where  # only a strict model gains anything
                counts["strict taken"] += 1
            elif not strict:
                # A lax model's refusal: JSON mode's errors, at the same places, worded and typed as
                # python mode's but for the rows of REWORDED and RETYPED.
                assert now == as_json, where
                assert [loc for loc, _ in now[1]] == [loc for loc, _ in today[1]], where
                if now[1:] == today[1:]:
                    counts["lax refused, same error"] += 1
                for (loc, was), (_, kind), said, says in zip(
                    today[1], now[1], today[2], now[2], strict=True
                ):
                    if was != kind:
                        assert (was, kind) in RETYPED, (where, was, kind)
                        given = payload[loc[0]]
                        assert isinstance(given, int) and abs(given) >= BIG, (where, given)
                        counts["lax refused, retyped"] += 1
                    elif said != says:
                        row = next(
                            (
                                name
                                for name, (old, new) in REWORDED.items()
                                if said.startswith(old) and says.startswith(new)
                            ),
                            None,
                        )
                        assert row is not None, (where, said, says)
                        reworded[row] += 1
            else:
                assert now == as_json, where  # refused: JSON mode's error
                counts["strict still refused"] += 1
    assert all(count >= 40 for count in counts.values()), counts
    assert all(count >= 10 for count in reworded.values()), reworded


def test_the_exception_a_strict_model_with_a_before_or_wrap_validator_still_refuses_its_own_dump():
    """That is pydantic's own JSON mode: such a validator hands the strict check a str, and it refuses.

    Stated in the docs. It was refused before this change and is refused after it.
    """

    class Before(BaseModel):
        model_config = ConfigDict(strict=True)

        when: datetime.datetime

        @field_validator("when", mode="before")
        @classmethod
        def _same(cls, value):
            return value

    class Wrapped(BaseModel):
        model_config = ConfigDict(strict=True)

        when: datetime.datetime

        @field_validator("when", mode="wrap")
        @classmethod
        def _same(cls, value, handler):
            return handler(value)

    class After(BaseModel):
        model_config = ConfigDict(strict=True)

        when: datetime.datetime

        @field_validator("when", mode="after")
        @classmethod
        def _same(cls, value):
            return value

    dump = {"when": "2026-01-02T03:04:05"}
    for model in (Before, Wrapped):
        with pytest.raises(ValidationError):
            validate_decoded(model, dump)
    assert validate_decoded(After, dump).when == WHEN
