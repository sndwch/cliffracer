"""An aware datetime is sent when the receiver reads back the instant the caller held.

The wire carries an instant and an offset. A value in a zone arrives in a fixed offset, and between
two zones `==` is always False for a value in a repeated hour, whatever the instants, so the read
back is judged by instant. A wall time the zone does not have (the hour skipped at the start of
daylight saving time) is refused by name: it arrives as the hour the zone shows for that instant.
"""

from __future__ import annotations

import dataclasses
import json
from datetime import UTC, datetime, timedelta, timezone, tzinfo
from zoneinfo import ZoneInfo

import pytest
from pydantic import BaseModel

from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import _same, wire_models

pytestmark = pytest.mark.unit

CHICAGO = ZoneInfo("America/Chicago")
#: 01:30 on this day happens twice in Chicago: in daylight time (fold=0) and an hour later in
#: standard time (fold=1).
REPEATED = datetime(2026, 11, 1, 1, 30, tzinfo=CHICAGO)
#: 02:30 on this day does not happen in Chicago: the clocks go from 02:00 to 03:00.
SKIPPED = datetime(2026, 3, 8, 2, 30, tzinfo=CHICAGO)


class Meeting(BaseModel):
    at: datetime


class Meetings(BaseModel):
    at: list[datetime]


@pytest.mark.parametrize(
    ("value", "wire"),
    [
        (REPEATED, "2026-11-01T01:30:00-05:00"),
        (REPEATED.replace(fold=1), "2026-11-01T01:30:00-06:00"),
    ],
    ids=["first-reading", "second-reading"],
)
def test_a_value_in_a_repeated_hour_is_sent_with_its_own_offset(value: datetime, wire: str):
    assert wire_models(Meeting(at=value)) == {"at": wire}
    arrived = Meeting.model_validate({"at": wire}).at
    assert arrived.astimezone(UTC) == value.astimezone(UTC)


def test_CONTROL_a_value_outside_any_transition_is_sent():
    assert wire_models(Meeting(at=datetime(2026, 7, 1, tzinfo=CHICAGO))) == {
        "at": "2026-07-01T00:00:00-05:00"
    }


def test_the_two_readings_of_a_repeated_hour_are_different_values():
    """`==` in one zone compares wall times, and calls these equal; they are an hour apart."""
    second = REPEATED.replace(fold=1)
    assert REPEATED == second, "the premise: one zone's == ignores fold"
    assert not _same(REPEATED, second)
    assert _same(REPEATED, REPEATED.astimezone(timezone(timedelta(hours=-5))))
    assert _same(second, second.astimezone(timezone(timedelta(hours=-6))))


SKIPPED_REFUSAL = (
    "refused before sending: Meeting would arrive with at read as 2026-03-08 03:30 in "
    "America/Chicago, since 02:30 on that day is an hour America/Chicago does not have, and no "
    "form of it is read back as the argument"
)


def test_a_wall_time_its_zone_does_not_have_is_refused_naming_the_hour_it_arrives_as():
    with pytest.raises(RpcValidationError) as caught:
        wire_models(Meeting(at=SKIPPED))
    assert caught.value.message == SKIPPED_REFUSAL
    assert [(d["type"], d["loc"]) for d in caught.value.details] == [
        ("value_would_be_misread", ["at"])
    ]


def test_a_wall_time_its_zone_does_not_have_is_named_inside_a_list():
    with pytest.raises(RpcValidationError) as caught:
        wire_models(Meetings(at=[datetime(2026, 7, 1, tzinfo=CHICAGO), SKIPPED]))
    assert "at read as 2026-03-08 03:30 in America/Chicago" in caught.value.message


def test_an_offset_finer_than_a_minute_is_still_refused():
    """The wire writes the offset to the minute, so the instant moves: a value is lost."""
    with pytest.raises(RpcValidationError) as caught:
        wire_models(Meeting(at=datetime(2026, 7, 1, tzinfo=timezone(timedelta(seconds=30)))))
    assert "at read as another value" in caught.value.message


class Inner(BaseModel):
    at: datetime


class OuterWithAList(BaseModel):
    inner: Inner
    many: list[Inner]


@dataclasses.dataclass
class Slot:
    at: datetime


class HoldsADataclass(BaseModel):
    dc: Slot


class HoldsDataclassesInAList(BaseModel):
    slots: list[Slot]


SUMMER = datetime(2026, 7, 1, tzinfo=CHICAGO)
NAMES_THE_GAP = "read as 2026-03-08 03:30 in America/Chicago, since 02:30 on that day is an hour"


@pytest.mark.parametrize(
    ("value", "field"),
    [
        (OuterWithAList(inner=Inner(at=SUMMER), many=[Inner(at=SKIPPED)]), "many"),
        (HoldsADataclass(dc=Slot(at=SKIPPED)), "dc"),
        (HoldsDataclassesInAList(slots=[Slot(at=SUMMER), Slot(at=SKIPPED)]), "slots"),
    ],
    ids=["a-model-in-a-list", "a-dataclass-field", "a-dataclass-in-a-list"],
)
def test_a_wall_time_its_zone_does_not_have_is_named_wherever_it_is_held(value, field):
    with pytest.raises(RpcValidationError) as caught:
        wire_models(value)
    assert f"{field} {NAMES_THE_GAP}" in caught.value.message, caught.value.message


class Midwest(tzinfo):
    """Chicago's offsets under a name of the caller's own, with no `key`: its 02:30 on 2026-03-08 is
    skipped as Chicago's is."""

    def utcoffset(self, dt):
        return CHICAGO.utcoffset(dt.replace(tzinfo=None))

    def dst(self, dt):
        return CHICAGO.dst(dt.replace(tzinfo=None))

    def tzname(self, dt):
        return "Midwest"

    def fromutc(self, dt):
        return CHICAGO.fromutc(dt.replace(tzinfo=CHICAGO)).replace(tzinfo=self)


def test_a_skipped_hour_in_a_zone_of_the_callers_own_is_named_by_its_name():
    with pytest.raises(RpcValidationError) as caught:
        wire_models(Meeting(at=datetime(2026, 3, 8, 2, 30, tzinfo=Midwest())))
    assert (
        "at read as 2026-03-08 03:30 in Midwest, since 02:30 on that day is an hour Midwest does "
        "not have" in caught.value.message
    )


class InASet(BaseModel):
    at: set[datetime]


class InAFrozenset(BaseModel):
    at: frozenset[datetime]


@pytest.mark.parametrize("model", [InASet, InAFrozenset], ids=["set", "frozenset"])
@pytest.mark.parametrize(
    ("value", "wire"),
    [
        (REPEATED, "2026-11-01T01:30:00-05:00"),
        (REPEATED.replace(fold=1), "2026-11-01T01:30:00-06:00"),
    ],
    ids=["first-reading", "second-reading"],
)
def test_a_value_in_a_repeated_hour_is_sent_from_a_set(model, value, wire):
    """A set is compared by its members' hash and `==`, and neither follows the instant: in a
    repeated hour the read-back member hashes alike and compares unequal (fold 0), or hashes
    differently (fold 1). Its members are judged by instant, as a field's value is."""
    assert wire_models(model(at={value})) == {"at": [wire]}
    (arrived,) = model.model_validate({"at": [wire]}).at
    assert arrived.astimezone(UTC) == value.astimezone(UTC)


def test_CONTROL_a_wall_time_its_zone_does_not_have_is_still_named_in_a_set():
    with pytest.raises(RpcValidationError) as caught:
        wire_models(InASet(at={SKIPPED}))
    assert f"at {NAMES_THE_GAP}" in caught.value.message


def test_CONTROL_an_offset_finer_than_a_minute_is_still_refused_in_a_set():
    """Its instant moves on the wire, so judging members by instant must still refuse it."""
    with pytest.raises(RpcValidationError) as caught:
        wire_models(InASet(at={datetime(2026, 7, 1, tzinfo=timezone(timedelta(seconds=30)))}))
    assert "at read as another value" in caught.value.message


class InNestedSets(BaseModel):
    at: frozenset[frozenset[datetime]]


class InSetsThreeDeep(BaseModel):
    at: frozenset[frozenset[frozenset[datetime]]]


class InTuplesInASet(BaseModel):
    at: set[tuple[datetime, int]]


@pytest.mark.parametrize(
    ("model", "hold"),
    [
        (InNestedSets, lambda v: frozenset({frozenset({v})})),
        (InSetsThreeDeep, lambda v: frozenset({frozenset({frozenset({v})})})),
        (InTuplesInASet, lambda v: {(v, 1)}),
    ],
    ids=["two-deep", "three-deep", "tuple-in-a-set"],
)
@pytest.mark.parametrize(
    ("value", "offset"),
    [(REPEATED, "-05:00"), (REPEATED.replace(fold=1), "-06:00")],
    ids=["first-reading", "second-reading"],
)
def test_a_value_in_a_repeated_hour_is_sent_from_inside_a_set_at_any_depth(
    model, hold, value, offset
):
    """A set's members are judged by instant however they hold the datetime: an inner set and a
    tuple are compared by Python's `==` and hash, which do not follow the instant either."""
    wire = wire_models(model(at=hold(value)))
    assert f"2026-11-01T01:30:00{offset}" in json.dumps(wire), wire


def test_CONTROL_a_wall_time_its_zone_does_not_have_is_still_named_two_sets_deep():
    with pytest.raises(RpcValidationError) as caught:
        wire_models(InNestedSets(at=frozenset({frozenset({SKIPPED})})))
    assert f"at {NAMES_THE_GAP}" in caught.value.message
