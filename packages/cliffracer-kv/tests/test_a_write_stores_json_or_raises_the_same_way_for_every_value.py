"""`put()` and `put_object()` store the same JSON, and refuse the same values.

`put_object()` serialised only models, dicts, lists, strings, numbers and bytearrays itself and
handed everything else to nats-py, so a dataclass, a tuple, a set, a datetime, a UUID, a decimal or
an enum, all stored as JSON by `put()`, raised nats-py's `TypeError: nats: invalid type for object
store`, naming no type. `serialize_value` stored an iterator, a generator or a file object as the
array of its items and left it consumed, and wrote `NaN` and `Infinity`, which are not JSON.
"""

from __future__ import annotations

import io
import json
import uuid
from dataclasses import dataclass
from datetime import date, datetime
from decimal import Decimal
from enum import Enum
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from cliffracer_kv import KvExtension
from cliffracer_kv.serialization import serialize_value

pytestmark = pytest.mark.unit


@dataclass
class Point:
    x: int
    y: int


class Color(Enum):
    RED = "red"


class Plain:
    pass


VALUES = {
    "dataclass": Point(1, 2),
    "tuple": (1, 2),
    "set": {1},
    "datetime": datetime(2026, 1, 1, 12, 0, 0),
    "date": date(2026, 1, 1),
    "uuid": uuid.UUID(int=1),
    "decimal": Decimal("1.5"),
    "enum": Color.RED,
    "nested": {"points": [Point(1, 2)], "when": datetime(2026, 1, 1)},
}


def _extension_with_store() -> tuple[KvExtension, AsyncMock]:
    store = AsyncMock()
    js = AsyncMock()
    js.object_store.return_value = store
    return KvExtension(object_stores=["media"], js=js), store


# --- put_object stores what put stores ---------------------------------------------------------


@pytest.mark.asyncio
@pytest.mark.parametrize("value", VALUES.values(), ids=VALUES.keys())
async def test_put_object_stores_the_json_put_stores(value):
    extension, store = _extension_with_store()

    await extension.put_object("media", "o", value)

    assert store.put.await_args.args[1] == serialize_value(value)
    json.loads(store.put.await_args.args[1])


@pytest.mark.asyncio
async def test_put_object_names_the_type_of_a_value_with_no_json_form():
    extension, store = _extension_with_store()

    with pytest.raises(TypeError, match="Plain"):
        await extension.put_object("media", "o", Plain())

    store.put.assert_not_awaited()


@pytest.mark.asyncio
async def test_CONTROL_bytes_and_a_stream_go_to_nats_py_as_they_are():
    extension, store = _extension_with_store()
    payload, stream = b"\x00\x01", io.BytesIO(b"abc")

    await extension.put_object("media", "a", payload)
    await extension.put_object("media", "b", stream)

    assert store.put.await_args_list[0].args[1] is payload
    assert store.put.await_args_list[1].args[1] is stream


@pytest.mark.asyncio
async def test_CONTROL_a_string_is_its_utf8_text():
    extension, store = _extension_with_store()

    await extension.put_object("media", "o", "héllo")

    assert store.put.await_args.args[1] == "héllo".encode()


# --- a value that is read by being consumed is refused -------------------------------------------


@pytest.mark.parametrize(
    "make",
    [
        lambda: iter([1, 2]),
        lambda: (i for i in range(3)),
        lambda: io.BytesIO(b"a\nb"),
        lambda: io.StringIO("l1\nl2\n"),
    ],
    ids=["iterator", "generator", "bytes-io", "string-io"],
)
def test_an_iterator_or_a_file_object_is_refused_naming_its_type_and_is_not_consumed(make):
    value = make()

    with pytest.raises(TypeError) as caught:
        serialize_value(value)

    assert type(value).__name__ in str(caught.value), str(caught.value)
    if hasattr(value, "tell"):
        assert value.tell() == 0


def test_a_consumable_inside_what_is_stored_is_refused_too():
    with pytest.raises(TypeError, match="generator"):
        serialize_value({"rows": (i for i in range(3))})


def test_an_open_file_is_refused_and_left_unread(tmp_path):
    path = tmp_path / "f.txt"
    path.write_bytes(b"abc")
    with path.open("rb") as handle, pytest.raises(TypeError):
        serialize_value(handle)
    with path.open("rb") as handle:
        try:
            serialize_value(handle)
        except TypeError:
            pass
        assert handle.read() == b"abc"


def test_CONTROL_a_list_and_a_dict_of_ordinary_values_are_stored_as_before():
    assert serialize_value([1, "a", None]) == b'[1, "a", null]'
    assert serialize_value({"a": 1}) == b'{"a": 1}'


# --- a number JSON has no spelling for is refused -----------------------------------------------


@pytest.mark.parametrize(
    "value",
    [float("nan"), float("inf"), float("-inf"), {"x": float("nan")}, [1.0, float("inf")]],
    ids=["nan", "inf", "-inf", "nan-in-dict", "inf-in-list"],
)
def test_a_number_that_is_not_json_is_refused(value):
    with pytest.raises(TypeError):
        serialize_value(value)


def test_CONTROL_ordinary_floats_are_stored_as_before():
    assert serialize_value(1.5) == b"1.5"
    assert serialize_value({"x": 0.0}) == b'{"x": 0.0}'


# --- what pydantic writes as a string is stored as that string -----------------------------------


def test_a_path_is_stored_as_its_string():
    assert json.loads(serialize_value(Path("/tmp/x"))) == "/tmp/x"
