"""A description read back from its wire form is the description that was written.

`to_dict` and `from_dict` are written separately, so a field one of them names and the other
does not is lost silently in between. Comparing `canonical(restored.to_dict())` with the dict it
was restored from cannot see that: both sides are renderings, and a field `to_dict` never emits is
missing from both. These compare the objects, and each class is built with a non-default value in
every one of its fields, so a field the round trip drops is a field that comes back as its default.
"""

from __future__ import annotations

import dataclasses
from typing import Any

import pytest

from cliffracer.introspect import (
    Description,
    EventListenerDescription,
    Method,
    Param,
    StreamDescription,
)

pytestmark = pytest.mark.unit

_SCHEMA = {"kind": "ref", "name": "Evt", "schema_hash": "sha256:feed"}

POPULATED: dict[type, dict[str, Any]] = {
    Param: {
        "name": "limit",
        "type": {"kind": "scalar", "name": "int"},
        "has_default": True,
        "default": 7,
        "rebuildable": True,
    },
    Method: {
        "name": "m",
        "doc": "d",
        "params": [Param("x", {"kind": "scalar", "name": "str"})],
        "returns": {"kind": "scalar", "name": "int"},
        "signature_hash": "sha256:abc",
        "doc_summary": "summary",
        "description": "the whole docstring",
    },
    EventListenerDescription: {
        "pattern": "evt.a",
        "handler_name": "on_a",
        "schema": _SCHEMA,
        "durable": "worker",
        "fanout": True,
        "pull": True,
        "queue_group": "q",
        "doc": "d",
        "doc_summary": "summary",
        "description": "the whole docstring",
        "cross_namespace": True,
        "effective_subject": "*.evt.a",
    },
    StreamDescription: {
        "name": "EVT",
        "subjects": ["evt.>"],
        "storage": "memory",
        "retention": "interest",
        "max_age_seconds": 60.0,
        "duplicate_window_seconds": 30.0,
    },
}


def _default_of(field: dataclasses.Field[Any]) -> Any:
    if field.default is not dataclasses.MISSING:
        return field.default
    if field.default_factory is not dataclasses.MISSING:
        return field.default_factory()
    return dataclasses.MISSING


@pytest.mark.parametrize("cls", list(POPULATED), ids=lambda cls: cls.__name__)
def test_the_fixture_gives_every_field_of_the_class_a_value_that_is_not_its_default(cls):
    """The instrument: a round trip over defaults proves nothing about a field left at its own."""
    values = POPULATED[cls]
    fields = [f for f in dataclasses.fields(cls) if f.init]

    assert sorted(values) == sorted(f.name for f in fields)
    assert [f.name for f in fields if values[f.name] == _default_of(f)] == []


@pytest.mark.parametrize("cls", list(POPULATED), ids=lambda cls: cls.__name__)
def test_a_class_built_with_every_field_set_is_equal_after_the_round_trip(cls):
    original = cls(**POPULATED[cls])

    restored = cls.from_dict(original.to_dict())  # type: ignore[attr-defined]

    assert restored == original


def test_a_description_carrying_all_of_them_is_equal_after_the_round_trip():
    original = Description(
        service="s",
        version="2",
        methods=[Method(**POPULATED[Method])],
        description_hash="sha256:0",
        components={"Evt": {"type": "object"}},
        listeners=[EventListenerDescription(**POPULATED[EventListenerDescription])],
        streams=[StreamDescription(**POPULATED[StreamDescription])],
    )

    assert Description.from_dict(original.to_dict()) == original


def test_CONTROL_a_field_the_wire_form_drops_is_found_by_the_comparison():
    """The comparison used above is red for a field that `to_dict` leaves out."""
    original = EventListenerDescription(**POPULATED[EventListenerDescription])
    wire = original.to_dict()
    del wire["durable"]

    assert EventListenerDescription.from_dict(wire) != original
