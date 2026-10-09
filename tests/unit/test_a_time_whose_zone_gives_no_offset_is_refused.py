"""A `time` whose zone gives it no offset is refused before sending, by name.

A `time` has no date, so a zone that needs one (a `ZoneInfo`) gives it no offset, and the dump
writes the wall time alone. The receiver would get a naive time with no sign the zone was dropped,
so the value is lost. Python's `==` calls the two equal only because it treats an aware time with
no offset as naive. A fixed offset is written with the time, and a naive time has nothing to lose.
"""

from __future__ import annotations

from datetime import datetime, time, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel, field_validator

from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import wire_models

pytestmark = pytest.mark.unit

CHICAGO = ZoneInfo("America/Chicago")
ZONED = time(1, 30, tzinfo=CHICAGO)


class Opening(BaseModel):
    at: time


class Openings(BaseModel):
    at: list[time]


class CheckedOpening(BaseModel):
    """Not a plain model (it has a validator), so it is sent only after a read-back."""

    at: time

    @field_validator("at")
    @classmethod
    def _unchanged(cls, value: time) -> time:
        return value


ZONED_REFUSAL = (
    "refused before sending: Opening would arrive with at read as 01:30 with no zone, since "
    "America/Chicago gives a time no offset without a date, and no form of it is read back as the "
    "argument"
)


def test_a_time_whose_zone_gives_no_offset_is_refused_by_name():
    assert ZONED.utcoffset() is None, "the premise: a ZoneInfo gives a time no offset"
    with pytest.raises(RpcValidationError) as caught:
        wire_models(Opening(at=ZONED))
    assert caught.value.message == ZONED_REFUSAL
    assert [(d["type"], d["loc"]) for d in caught.value.details] == [
        ("value_would_be_misread", ["at"])
    ]


def test_a_time_whose_zone_gives_no_offset_is_named_inside_a_list():
    with pytest.raises(RpcValidationError) as caught:
        wire_models(Openings(at=[time(9, 0), ZONED]))
    assert "at read as 01:30 with no zone, since America/Chicago gives" in caught.value.message


@pytest.mark.parametrize("model", [Opening, CheckedOpening], ids=["plain", "read-back"])
def test_a_time_with_a_fixed_offset_is_sent_with_it(model):
    fixed = time(1, 30, tzinfo=timezone(timedelta(hours=-5)))
    assert wire_models(model(at=fixed)) == {"at": "01:30:00-05:00"}


@pytest.mark.parametrize("model", [Opening, CheckedOpening], ids=["plain", "read-back"])
def test_a_naive_time_is_sent(model):
    assert wire_models(model(at=time(1, 30))) == {"at": "01:30:00"}


class Named(tzinfo):
    """A zone of the caller's own, with a name and no offset for a time."""

    def utcoffset(self, dt):
        return None

    def tzname(self, dt):
        return "Shop hours"

    def dst(self, dt):
        return None


class Nameless(Named):
    def tzname(self, dt):
        return None


def test_a_zone_of_the_callers_own_is_named_by_its_name():
    with pytest.raises(RpcValidationError) as caught:
        wire_models(Opening(at=time(1, 30, tzinfo=Named())))
    assert "at read as 01:30 with no zone, since Shop hours gives a time no offset" in (
        caught.value.message
    )


def test_a_zone_with_no_key_and_no_name_is_named_by_its_repr():
    zone = Nameless()
    with pytest.raises(RpcValidationError) as caught:
        wire_models(Opening(at=time(1, 30, tzinfo=zone)))
    assert f"since {zone!r} gives a time no offset" in caught.value.message


SUB_SECOND = timezone(timedelta(microseconds=5))


@pytest.mark.parametrize("model", [Opening, CheckedOpening], ids=["plain", "read-back"])
def test_a_time_whose_offset_has_a_sub_second_part_is_refused(model):
    """The dump writes an offset to the minute, so `+00:00:00.000005` arrives as UTC, and `==`
    calls the two times equal; the time is refused as read as another value, as the same offset on
    a datetime is."""
    with pytest.raises(RpcValidationError) as caught:
        wire_models(model(at=time(3, 4, 5, 6, tzinfo=SUB_SECOND)))
    assert [(d["type"], d["loc"]) for d in caught.value.details] == [
        ("value_would_be_misread", ["at"])
    ]


class OpeningSet(BaseModel):
    at: set[time]


class Shop(BaseModel):
    opening: Opening


@pytest.mark.parametrize(
    "make",
    [
        pytest.param(lambda: Openings(at=[time(9, 0), time(3, tzinfo=SUB_SECOND)]), id="a-list"),
        pytest.param(lambda: OpeningSet(at={time(3, tzinfo=SUB_SECOND)}), id="a-set"),
        pytest.param(lambda: Shop(opening=Opening(at=time(3, tzinfo=SUB_SECOND))), id="a-model"),
    ],
)
def test_a_time_whose_offset_has_a_sub_second_part_is_refused_where_it_is_held(make):
    with pytest.raises(RpcValidationError):
        wire_models(make())


class Meeting(BaseModel):
    at: datetime


@pytest.mark.parametrize(
    ("value", "model"),
    [
        pytest.param(
            datetime(2026, 1, 2, 3, tzinfo=SUB_SECOND), Meeting, id="a-datetime-sub-second"
        ),
        pytest.param(time(3, tzinfo=timezone(timedelta(seconds=30))), Opening, id="a-time-30s"),
    ],
)
def test_CONTROL_an_offset_the_dump_cannot_write_is_still_refused(value, model):
    with pytest.raises(RpcValidationError):
        wire_models(model(at=value))
