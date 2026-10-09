"""A stream declaration states its message and byte limits, its discard policy and its replicas.

Each is optional. Left out, nothing changes: it is not sent when the stream is created, an update
keeps the operator's value, and it is not compared. Declared, it is sent, compared with what the
broker reports back, and refused where it is impossible or ambiguous. What each broker reports was
measured on nats-server 2.10.29, 2.11.2 and 2.12.0: a limit left out, sent as 0 or as -1 comes
back as -1; no discard comes back "old"; no replica count, or 0, comes back 1.
"""

from types import SimpleNamespace

import pytest
from nats.js.api import DiscardPolicy, StreamConfig
from pydantic import ValidationError

from cliffracer.core.jetstream import StreamSpec

pytestmark = pytest.mark.unit


def spec(**fields) -> StreamSpec:
    return StreamSpec(name="EVENTS", subjects=["events.>"], **fields)


@pytest.mark.parametrize(
    ("fields", "said"),
    [
        pytest.param({"max_msgs": 0}, "max_msgs is 0", id="max-msgs-zero"),
        pytest.param({"max_msgs": -1}, "max_msgs is -1", id="max-msgs-minus-one"),
        pytest.param({"max_bytes": 0}, "max_bytes is 0", id="max-bytes-zero"),
        pytest.param({"max_bytes": -1}, "max_bytes is -1", id="max-bytes-minus-one"),
        pytest.param({"num_replicas": 0}, "num_replicas is 0", id="replicas-zero"),
        pytest.param({"num_replicas": 6}, "num_replicas is 6", id="replicas-six"),
        pytest.param(
            {"discard": "new"}, "discard='new' refuses a message only", id="discard-new-alone"
        ),
    ],
)
def test_an_impossible_or_ambiguous_value_is_refused_naming_it(fields, said):
    with pytest.raises(ValidationError, match=said):
        spec(**fields)


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param({"max_msgs": 1, "max_bytes": 1}, id="the-smallest-limits"),
        pytest.param({"num_replicas": 1}, id="one-replica"),
        pytest.param({"num_replicas": 5}, id="five-replicas"),
        pytest.param({"discard": "new", "max_msgs": 10}, id="discard-new-with-a-message-limit"),
        pytest.param({"discard": "new", "max_bytes": 10}, id="discard-new-with-a-byte-limit"),
        pytest.param({"discard": "old"}, id="discard-old-alone"),
    ],
)
def test_CONTROL_a_possible_value_is_declared(fields):
    assert spec(**fields).declaration_problem() is None


def test_a_declared_value_is_sent_and_one_left_out_is_not():
    declared = spec(max_msgs=10, max_bytes=1000, discard="new", num_replicas=3).to_stream_config()
    left_out = spec().to_stream_config()

    assert (declared.max_msgs, declared.max_bytes, declared.discard, declared.num_replicas) == (
        10,
        1000,
        DiscardPolicy.NEW,
        3,
    )
    # nats-py's own default for discard is "old", the server's default too, so a declaration that
    # leaves it out asks for what it always did.
    assert (left_out.max_msgs, left_out.max_bytes, left_out.discard, left_out.num_replicas) == (
        None,
        None,
        DiscardPolicy.OLD,
        None,
    )


def test_an_update_keeps_the_operators_limits_where_none_is_declared_and_overlays_those_that_are():
    operators = StreamConfig(
        name="EVENTS",
        subjects=["events.>"],
        max_msgs=500,
        max_bytes=50_000,
        discard=DiscardPolicy.NEW,
        num_replicas=3,
    )

    kept = spec().apply_to(operators)
    overlaid = spec(max_msgs=100).apply_to(operators)

    assert (kept.max_msgs, kept.max_bytes, kept.discard, kept.num_replicas) == (
        500,
        50_000,
        DiscardPolicy.NEW,
        3,
    )
    assert (overlaid.max_msgs, overlaid.max_bytes) == (100, 50_000)


@pytest.mark.parametrize("reported", [-1, 0, None], ids=["minus-one", "zero", "absent"])
def test_a_limit_the_broker_reports_as_none_is_no_limit(reported):
    broker = StreamConfig(name="EVENTS", subjects=["events.>"], max_msgs=reported)

    assert spec(max_msgs=10).declared_differences(broker) == [("max_msgs", 10, "no limit")]
    assert spec().declared_differences(broker) == []


def test_no_discard_reported_is_old_and_no_replica_count_or_zero_is_one():
    for reported_replicas in (None, 0):
        broker = StreamConfig(name="EVENTS", subjects=["events.>"], num_replicas=reported_replicas)
        assert spec(discard="old", num_replicas=1).declared_differences(broker) == []
    assert spec(discard="new", max_msgs=1).declared_differences(
        StreamConfig(name="EVENTS", subjects=["events.>"], max_msgs=1)
    ) == [("discard", "new", "old")]


def test_more_replicas_than_declared_is_not_drift_and_fewer_is():
    three = StreamConfig(name="EVENTS", subjects=["events.>"], num_replicas=3)

    assert spec(num_replicas=1).declared_differences(three) == []
    assert spec(num_replicas=5).declared_differences(three) == [("num_replicas", 5, 3)]


def test_a_declared_limit_below_what_the_stream_holds_is_named():
    held = SimpleNamespace(messages=10, bytes=1410)

    assert spec(max_msgs=5, max_bytes=1000).limit_below_usage(held) == [
        ("max_msgs", 5, 10),
        ("max_bytes", 1000, 1410),
    ]
    assert spec(max_msgs=10, max_bytes=1410).limit_below_usage(held) == []
    assert spec().limit_below_usage(held) == []


def _info(configured, cluster):
    return SimpleNamespace(
        config=StreamConfig(name="EVENTS", num_replicas=configured), cluster=cluster
    )


@pytest.mark.parametrize(
    ("configured", "cluster", "said"),
    [
        pytest.param(
            3,
            SimpleNamespace(name=None, leader="n1", replicas=None),
            "configured 3, 1 live, not clustered",
            id="a-single-server-that-accepted-the-update",
        ),
        pytest.param(3, None, "configured 3, 1 live, not clustered", id="no-cluster-information"),
        pytest.param(
            1,
            SimpleNamespace(name="c", leader="n1", replicas=[]),
            "configured 1, 1 live",
            id="a-cluster-kept-at-one",
        ),
        pytest.param(
            3,
            SimpleNamespace(name="c", leader="n1", replicas=[object()]),
            "configured 3, 2 live",
            id="a-cluster-with-one-peer",
        ),
    ],
)
def test_a_stream_keeping_fewer_copies_than_declared_is_named(configured, cluster, said):
    assert spec(num_replicas=3).replica_shortfall(_info(configured, cluster)).endswith(said)


def test_CONTROL_a_lagging_peer_counts_and_undeclared_replicas_are_not_checked():
    lagging = SimpleNamespace(
        name="c",
        leader="n1",
        replicas=[SimpleNamespace(current=False), SimpleNamespace(current=True)],
    )

    assert spec(num_replicas=3).replica_shortfall(_info(3, lagging)) is None
    assert spec().replica_shortfall(_info(3, None)) is None
