"""A declaration the client or server would refuse is refused before the first stream is created.

`ensure_streams` decides first and creates after, so that a conflict in a later declaration does not
leave the earlier ones on a shared broker. It checked overlap and drift only: a name nats-py refuses,
a subject the server refuses and a duration that is not a number of seconds all failed inside the
apply loop, after the declarations before them were created, as a builtin `ValueError` or a
`ServerError` that named no declaration. Each is now refused first, as a `StreamDeclarationError`
naming the stream, and a `StreamSpec` that holds one cannot be built.

The JetStream context here records what it was asked to create, so "nothing was created" is read
from the calls the context received.
"""

import math
from typing import Any

import pytest
from pydantic import ValidationError

from cliffracer import ServiceConfig, StreamSpec
from cliffracer.core.jetstream import StreamDeclarationError, ensure_streams

pytestmark = pytest.mark.unit


class _Page:
    total = 0

    def __iter__(self):
        return iter(())


class RecordingJetStream:
    """A broker with no streams that records every create and update it is asked for."""

    def __init__(self) -> None:
        self.added: list[str] = []
        self.updated: list[str] = []

    async def streams_info_iterator(self, offset: int = 0) -> _Page:
        return _Page()

    async def add_stream(self, config: Any) -> None:
        self.added.append(config.name)

    async def update_stream(self, config: Any) -> None:
        self.updated.append(config.name)


def _unvalidated(**fields: Any) -> StreamSpec:
    """A spec that skipped validation, as an assignment or `model_construct` produces."""
    return StreamSpec.model_construct(
        **{
            "storage": "file",
            "retention": "limits",
            "max_age_seconds": None,
            "duplicate_window_seconds": 120.0,
            **fields,
        }
    )


def declared(**fields: Any) -> dict[str, Any]:
    """The fields of a declaration, for a case that is built, or not, by the test."""
    return fields


GOOD = StreamSpec(name="ORDERS", subjects=["orders.>"])

# (id, fields, text the refusal must hold)
REFUSED = [
    pytest.param(declared(name="BAD.NAME", subjects=["bad.>"]), "'.'", id="name-with-a-dot"),
    pytest.param(declared(name="BAD NAME", subjects=["bad.>"]), "' '", id="name-with-a-space"),
    pytest.param(declared(name="BAD/NAME", subjects=["bad.>"]), "'/'", id="name-with-a-slash"),
    pytest.param(
        declared(name="BAD\\NAME", subjects=["bad.>"]), "\\\\", id="name-with-a-backslash"
    ),
    pytest.param(declared(name="BAD*", subjects=["bad.>"]), "'*'", id="name-with-a-star"),
    pytest.param(declared(name="BAD>", subjects=["bad.>"]), "'>'", id="name-with-a-chevron"),
    pytest.param(declared(name="BAD\tNAME", subjects=["bad.>"]), "\\t", id="name-with-a-tab"),
    pytest.param(declared(name="", subjects=["bad.>"]), "name is empty", id="empty-name"),
    pytest.param(
        declared(name="BAD", subjects=["bad..x"]), "empty token", id="subject-empty-token"
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad x"]), "white space", id="subject-with-a-space"
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad.\n"]), "white space", id="subject-with-a-newline"
    ),
    pytest.param(declared(name="BAD", subjects=[".bad"]), "empty token", id="subject-leading-dot"),
    pytest.param(
        declared(name="BAD", subjects=["bad.>.x"]), "'>'", id="subject-nonterminal-chevron"
    ),
    pytest.param(
        declared(name="BAD", subjects=["ok.>", "bad..x"]),
        "bad..x",
        id="second-subject-is-the-bad-one",
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad.>", "bad.x"]),
        "overlap",
        id="subjects-wildcard-overlaps",
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad.*.b", "bad.c.>"]),
        "overlap",
        id="subjects-cross-overlap",
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad.x", "bad.x"]), "repeat", id="subject-repeated"
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad.>"], max_age_seconds=-1),
        "max_age_seconds",
        id="max-age-negative",
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad.>"], max_age_seconds=math.nan),
        "max_age_seconds",
        id="max-age-nan",
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad.>"], duplicate_window_seconds=-1),
        "duplicate_window_seconds",
        id="window-negative",
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad.>"], duplicate_window_seconds=math.inf),
        "duplicate_window_seconds",
        id="window-infinite",
    ),
    pytest.param(declared(name="BAD", subjects=[""]), "is empty", id="subject-empty"),
    pytest.param(
        declared(name="BAD", subjects=["a\x7fb"]), "control character", id="subject-with-a-delete"
    ),
    pytest.param(
        declared(name="BAD", subjects=["bad.x", "bad.*"]),
        "overlap",
        id="a-star-in-the-second-subject-overlaps",
    ),
]


@pytest.mark.parametrize(("fields", "says"), REFUSED)
async def test_a_good_stream_followed_by_a_bad_one_creates_nothing(fields, says):
    js = RecordingJetStream()

    with pytest.raises(StreamDeclarationError) as raised:
        await ensure_streams(js, [GOOD, _unvalidated(**fields)])

    assert js.added == [] and js.updated == [], "a stream was created before the refusal"
    assert says in str(raised.value)
    assert repr(fields["name"]) in str(raised.value), "the refusal does not name the stream"


@pytest.mark.parametrize(("fields", "says"), REFUSED)
def test_a_spec_holding_it_cannot_be_built(fields, says):
    with pytest.raises(ValidationError) as raised:
        StreamSpec(**fields)

    assert says in str(raised.value)


async def test_every_refused_declaration_is_reported_at_once():
    js = RecordingJetStream()
    specs = [
        GOOD,
        _unvalidated(name="BAD.ONE", subjects=["one.>"]),
        _unvalidated(name="BAD", subjects=["two..x"]),
    ]

    with pytest.raises(StreamDeclarationError) as raised:
        await ensure_streams(js, specs)

    text = str(raised.value)
    assert "2 of 3 declared streams" in text and "'BAD.ONE'" in text and "two..x" in text
    assert js.added == []


async def test_a_spec_changed_after_it_was_built_is_refused_where_it_is_applied():
    js = RecordingJetStream()
    spec = StreamSpec(name="LATER", subjects=["later.>"])
    spec.name = "LATER.NAME"  # assignment is not validated

    with pytest.raises(StreamDeclarationError, match="LATER.NAME"):
        await ensure_streams(js, [GOOD, spec])

    assert js.added == []


def test_the_refusal_is_raised_when_the_service_config_is_built():
    with pytest.raises(ValidationError, match="BAD.NAME"):
        ServiceConfig(
            name="svc",
            jetstream_enabled=True,
            jetstream_streams=[StreamSpec(name="BAD.NAME", subjects=["bad.>"])],
        )


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(
            declared(name="ORDERS_V2-1", subjects=["orders_v2.>"]), id="name-with-_-and--"
        ),
        pytest.param(declared(name="A", subjects=["a.*.b", "x.>"]), id="inner-and-final-wildcards"),
        pytest.param(declared(name="A", subjects=["*"]), id="a-lone-star"),
        pytest.param(
            declared(name="A", subjects=["a.*.b", "a.*.c"]), id="subjects-that-do-not-overlap"
        ),
        pytest.param(declared(name="A", subjects=["a*b.c"]), id="a-star-inside-a-token-is-literal"),
        pytest.param(declared(name="A", subjects=["a.>"], max_age_seconds=None), id="no-age"),
        pytest.param(declared(name="A", subjects=["a.>"], max_age_seconds=0), id="age-zero"),
        pytest.param(
            declared(name="A", subjects=["a.>"], max_age_seconds=3600.5), id="fractional-age"
        ),
        pytest.param(
            declared(name="A", subjects=["a.>"], duplicate_window_seconds=0), id="window-zero"
        ),
        pytest.param(
            declared(name="A", subjects=["a.b", "a"]), id="a-longer-subject-before-its-prefix"
        ),
        pytest.param(
            declared(name="A", subjects=["a.>", "a"]), id="a-chevron-does-not-cover-its-bare-prefix"
        ),
        pytest.param(declared(name="A", subjects=["a!b.>"]), id="printable-punctuation"),
        pytest.param(declared(name="A", subjects=["a~b.>"]), id="the-last-printable-character"),
    ],
)
async def test_CONTROL_a_declaration_the_broker_accepts_is_still_declared(fields):
    js = RecordingJetStream()

    await ensure_streams(js, [GOOD, StreamSpec(**fields)])

    assert js.added == ["ORDERS", fields["name"]]
