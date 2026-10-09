"""What a stored value reads back as, which is the table in the README.

A string whose text is JSON reads back as the JSON value unless `as_type=str` asks for the text,
and an empty value is a key that exists, not an absent one.
"""

from __future__ import annotations

import math
from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from cliffracer_kv import KvExtension
from cliffracer_kv.serialization import deserialize_value, serialize_value

pytestmark = pytest.mark.unit

ABSENT = object()


@pytest.mark.parametrize(
    ("stored", "inferred"),
    [
        ("hello", "hello"),
        ("123", 123),
        ("3.5", 3.5),
        ("true", True),
        ("null", None),
        ('{"a": 1}', {"a": 1}),
        (b"hello", "hello"),
    ],
    ids=repr,
)
def test_a_value_with_no_type_reads_back_as_the_table_says(stored, inferred):
    assert deserialize_value(serialize_value(stored)) == inferred


def test_a_stored_nan_reads_back_as_nan():
    assert math.isnan(deserialize_value(serialize_value("NaN")))


@pytest.mark.parametrize("stored", ["hello", "123", "true", "null", '{"a": 1}', "3.5"], ids=repr)
def test_as_type_str_returns_exactly_the_text_that_was_stored(stored):
    assert deserialize_value(serialize_value(stored), as_type=str) == stored


@pytest.mark.parametrize("stored", [b"hello", b"123", b"\x00\x01\xff"], ids=repr)
def test_as_type_bytes_returns_exactly_the_bytes_that_were_stored(stored):
    assert deserialize_value(serialize_value(stored), as_type=bytes) == stored


def test_a_stored_json_null_and_an_absent_key_differ_only_when_the_default_does():
    """`"null"` reads back as None, and so does an absent key whose default is None."""
    assert deserialize_value(b"null", default=ABSENT) is None
    assert deserialize_value(None, default=ABSENT) is ABSENT


def _extension_holding(entry) -> KvExtension:
    kv = AsyncMock()
    kv.get.return_value = entry
    js = AsyncMock()
    js.key_value.return_value = kv
    return KvExtension(js=js)


def _entry(value) -> SimpleNamespace:
    return SimpleNamespace(value=value, operation=None, revision=1)


@pytest.mark.asyncio
async def test_a_key_stored_with_an_empty_value_reads_back_empty_not_as_the_default():
    """nats-py reports an empty payload's `value` as None; the entry is what shows the key exists."""
    ext = _extension_holding(_entry(None))

    assert await ext.get("b", "k", default=ABSENT) == ""


@pytest.mark.asyncio
async def test_an_empty_value_read_with_as_type_bytes_is_empty_bytes():
    ext = _extension_holding(_entry(None))

    assert await ext.get("b", "k", as_type=bytes, default=ABSENT) == b""


@pytest.mark.asyncio
async def test_an_empty_value_read_with_as_type_str_is_the_empty_string():
    ext = _extension_holding(_entry(None))

    assert await ext.get("b", "k", as_type=str, default=ABSENT) == ""


@pytest.mark.asyncio
async def test_CONTROL_a_missing_key_still_returns_the_default():
    ext = _extension_holding(None)

    assert await ext.get("b", "k", default=ABSENT) is ABSENT


@pytest.mark.asyncio
async def test_CONTROL_a_value_that_is_present_is_unchanged_by_the_empty_value_rule():
    ext = _extension_holding(_entry(b'{"a": 1}'))

    assert await ext.get("b", "k", default=ABSENT) == {"a": 1}
