"""Two rules the wire-form choice states and nothing else pinned.

A form that raises `FormUnavailable` is skipped: the next form is made and judged, and when none
is accepted the form sent is the first one actually made. And a comparison that raises is "not
equal", both in `_same`, which judges a value read back against the one sent, and in
`_reads_as_the_argument`, which asks whether a form reads back as the caller's model: a value
whose `==` raises cannot be shown to arrive unchanged, so it is never taken as arriving unchanged.
"""

import pytest
from pydantic import BaseModel, ConfigDict

from cliffracer.core.validation import (
    FormUnavailable,
    _reads_as_the_argument,
    _same,
    choose_wire_form,
)

pytestmark = pytest.mark.unit


def _unavailable():
    raise FormUnavailable


def _read_back(table):
    """A receiver that reads each wire as `table` says, refusing what `table` maps to an error."""

    def read(wire):
        back = table[wire]
        if isinstance(back, Exception):
            raise back
        return back

    return read


@pytest.mark.parametrize(
    ("forms", "table", "sent"),
    [
        pytest.param(
            [_unavailable, lambda: "b"], {"b": "value"}, "b", id="first-unavailable-next-reads-back"
        ),
        pytest.param(
            [lambda: "a", _unavailable, lambda: "c"],
            {"a": ValueError("refused"), "c": "value"},
            "c",
            id="unavailable-between-a-refused-and-an-accepted-form",
        ),
        pytest.param(
            [_unavailable, lambda: "b", lambda: "c"],
            {"b": ValueError("refused"), "c": ValueError("refused")},
            "b",
            id="none-accepted-the-first-made-is-sent",
        ),
    ],
)
def test_a_form_that_cannot_be_made_is_skipped(forms, table, sent):
    assert choose_wire_form("value", forms, _read_back(table)) == sent


class _Uncomparable:
    """A value whose `==` raises, as an array's does when asked for one truth value."""

    def __eq__(self, other):
        raise ValueError("the truth value of this comparison is ambiguous")

    __hash__ = object.__hash__


def test_a_comparison_that_raises_is_not_the_same():
    one, other = _Uncomparable(), _Uncomparable()

    assert _same(one, other) is False
    assert _same([1, one], [1, other]) is False
    assert _same(one, one), "the same object is the same without being compared"


class _HoldsAnUncomparable(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)

    n: int
    held: _Uncomparable


def test_a_model_holding_a_value_whose_comparison_raises_does_not_read_as_the_argument():
    """The raise is in a field's value, so it is met whichever way the model is compared, whole or
    field by field: the form reads back, and cannot be shown to read back as what was sent."""
    sent = _HoldsAnUncomparable(n=1, held=_Uncomparable())
    wire = {"n": 1, "held": _Uncomparable()}

    assert _HoldsAnUncomparable.model_validate(wire).n == 1, "the form itself is accepted"
    assert _reads_as_the_argument(_HoldsAnUncomparable, wire, sent) is False
