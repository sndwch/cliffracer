"""The inspector reads a stream through its message-get API and prints what it finds.

The stream manager here is a fake that answers the way nats-py's does, which was read from its
source: `get_msg(stream, seq=N, subject=S, next=True)` returns the first message at or after `N`
on a subject matching `S`, and `get_msg(stream, seq=N)` the message at `N`; either raises
`NotFoundError` when there is none. The fake has those three methods and nothing else, so a command
that called any other API would fail on it. Not run: a live stream.
"""

import datetime
import io
import json
from types import SimpleNamespace

import pytest
from cliffracer_dlq import cli
from cliffracer_dlq.reader import StreamChoiceError, read, read_one, resolve_stream, stream_names
from nats.js.api import RawStreamMsg
from nats.js.errors import NotFoundError, ServiceUnavailableError

from cliffracer.core.subjects import subject_matches

pytestmark = pytest.mark.unit

# A fixed instant: `--since` counts back from the clock the test hands the command, not the real one.
NOW = datetime.datetime(2026, 10, 2, 16, 0, 0, tzinfo=datetime.UTC)


def _raw(
    seq, record, *, subject="dlq.orders", minutes_ago=5, headers=None, data=None
) -> RawStreamMsg:
    return RawStreamMsg(
        subject=subject,
        seq=seq,
        data=data if data is not None else json.dumps(record).encode(),
        headers=headers if headers is not None else {"Content-Type": "application/json"},
        time=NOW - datetime.timedelta(minutes=minutes_ago),
    )


class FakeStreams:
    """Only the reads. Anything else raises AttributeError."""

    def __init__(self, messages, stream="DLQ", subjects=("dlq.*",), others=None):
        self.messages = {m.seq: m for m in messages}
        self.stream = stream
        self.subjects = subjects
        #: Other streams on the broker, by name, with the subjects each holds.
        self.others = others or {}
        self.calls: list[tuple] = []

    async def stream_names(self, subject):
        self.calls.append(("names", subject))
        held = {self.stream: self.subjects, **self.others}
        return [
            name
            for name, subjects in held.items()
            if any(subject_matches(s, subject) or subject_matches(subject, s) for s in subjects)
        ]

    async def stream_info(self, name):
        self.calls.append(("info", name))
        if name != self.stream:
            raise NotFoundError
        seqs = sorted(self.messages)
        return SimpleNamespace(
            state=SimpleNamespace(
                messages=len(seqs),
                first_seq=seqs[0] if seqs else 0,
                last_seq=seqs[-1] if seqs else 0,
            )
        )

    async def get_msg(self, stream_name, seq=None, subject=None, direct=False, next=False):
        self.calls.append(("get", stream_name, seq, subject, next))
        if next:
            for candidate in sorted(self.messages):
                message = self.messages[candidate]
                if candidate >= (seq or 0) and (
                    subject is None or subject_matches(subject, message.subject)
                ):
                    return message
            raise NotFoundError
        if seq in self.messages:
            return self.messages[seq]
        raise NotFoundError


LIMIT = {
    "original_subject": "events.order.created",
    "payload": {"number": 1},
    "error": "TypeError: unlucky",
    "service": "orders",
    "deliveries": 5,
    "stream": "EVENTS",
    "stream_sequence": 42,
    "consumer": "orders-durable",
}
INVALID = {
    "original_subject": "events.payment.created",
    "payload": {"amount": "x"},
    "errors": [
        {"type": "int_parsing", "loc": ["amount"], "msg": "Input should be a valid integer"}
    ],
    "service": "billing",
    "schema": "Payment",
}
DECODE = {
    "original_subject": "events.order.created",
    "payload": {"raw": "{"},
    "error": "Decode error: bad",
    "service": "orders",
    "deliveries": 1,
}


def _streams() -> FakeStreams:
    return FakeStreams(
        [
            _raw(3, LIMIT, minutes_ago=50),
            _raw(4, INVALID, minutes_ago=40),
            _raw(7, DECODE, minutes_ago=30),  # 5 and 6 were deleted: a gap
            _raw(8, None, data=b"\xff not a record", minutes_ago=20),
            _raw(9, {"x": 1}, subject="elsewhere.log", minutes_ago=15),  # on another subject
            _raw(10, LIMIT, minutes_ago=2),
        ]
    )


async def _run(*argv: str, streams: FakeStreams | None = None, now=None):
    args = cli.build_parser().parse_args(list(argv))
    out, err = io.StringIO(), io.StringIO()
    clock = (lambda: NOW) if now is None else now
    code = await cli.execute(args, streams or _streams(), out, err, now=clock)
    return code, out.getvalue(), err.getvalue()


async def test_the_whole_stream_is_read_oldest_first_across_a_gap_and_only_on_the_subject():
    letters = [dl async for dl in read(_streams(), "DLQ", "dlq.*")]

    assert [dl.sequence for dl in letters] == [3, 4, 7, 8, 10]


async def test_messages_after_the_last_one_on_the_subject_end_the_read_without_an_error():
    streams = FakeStreams([_raw(1, LIMIT), _raw(2, {"x": 1}, subject="elsewhere.log")])

    assert [dl.sequence async for dl in read(streams, "DLQ", "dlq.*")] == [1]


async def test_an_empty_stream_yields_nothing():
    assert [dl async for dl in read(FakeStreams([]), "DLQ", "dlq.*")] == []


async def test_the_read_asks_for_the_next_message_on_the_subject_and_nothing_else():
    streams = _streams()

    [dl async for dl in read(streams, "DLQ", "dlq.orders")]

    kinds = {call[0] for call in streams.calls}
    assert kinds == {"info", "get"}
    assert all(
        call[4] is True and call[3] == "dlq.orders" for call in streams.calls if call[0] == "get"
    )


async def test_read_one_returns_the_message_at_a_sequence_or_none():
    streams = _streams()

    found = await read_one(streams, "DLQ", 4)

    assert found is not None and found.cause == "invalid"
    assert await read_one(streams, "DLQ", 5) is None


async def test_a_named_stream_is_used_without_asking_the_broker():
    streams = _streams()

    assert await resolve_stream(streams, "OTHER", "dlq.*") == "OTHER"
    assert streams.calls == []


async def test_a_subject_finds_its_stream():
    assert await resolve_stream(_streams(), None, "dlq.*") == "DLQ"


def _per_service_streams() -> FakeStreams:
    """Two streams with disjoint subjects, as the broker requires, that one wildcard matches."""
    return FakeStreams(
        [_raw(1, LIMIT), _raw(2, DECODE)],
        stream="DLQ_A",
        subjects=("dlq.orders",),
        others={"DLQ_C": ("dlq.audit",), "DLQ_B": ("dlq.billing",)},
    )


async def test_a_wildcard_that_several_streams_hold_is_refused_naming_them():
    with pytest.raises(StreamChoiceError) as refused:
        await resolve_stream(_per_service_streams(), None, "dlq.*")

    assert "3 streams hold the subject 'dlq.*': DLQ_A, DLQ_B, DLQ_C" in str(refused.value)
    assert "--stream" in str(refused.value)


async def test_a_literal_subject_resolves_to_the_one_stream_that_holds_it():
    assert await resolve_stream(_per_service_streams(), None, "dlq.billing") == "DLQ_B"


async def test_a_subject_no_stream_holds_is_refused_naming_the_subject():
    with pytest.raises(StreamChoiceError, match="no stream holds the subject 'staging.dlq.\\*'"):
        await resolve_stream(_streams(), None, "staging.dlq.*")


async def test_ls_prints_one_line_per_record_with_what_a_reader_needs():
    code, out, err = await _run("ls")

    lines = out.splitlines()
    assert code == 0 and len(lines) == 5
    assert (
        "orders" in lines[0] and "delivery-limit" in lines[0] and "events.order.created" in lines[0]
    )
    assert "deliveries=5" in lines[0] and "TypeError: unlucky" in lines[0]
    assert (
        "billing" in lines[1]
        and "invalid" in lines[1]
        and "amount: Input should be a valid integer" in lines[1]
    )
    assert "decode" in lines[2]
    assert "unreadable" in lines[3] and "cannot decode the message" in lines[3]
    assert err.startswith("stream: DLQ  subject: dlq.*")


async def test_ls_limit_stops_and_says_there_is_more():
    code, out, err = await _run("ls", "--limit", "2")

    assert code == 0 and len(out.splitlines()) == 2
    assert "more match" in err


async def test_ls_limit_equal_to_the_matches_does_not_claim_more():
    _, out, err = await _run("ls", "--limit", "5")

    assert len(out.splitlines()) == 5 and "more match" not in err


async def test_ls_json_is_one_object_per_line_with_the_fields_a_script_reads():
    _, out, _ = await _run("ls", "--json")

    rows = [json.loads(line) for line in out.splitlines()]
    assert [r["sequence"] for r in rows] == [3, 4, 7, 8, 10]
    first = rows[0]
    assert first["cause"] == "delivery-limit" and first["service"] == "orders"
    assert (first["stream"], first["stream_sequence"], first["consumer"]) == (
        "EVENTS",
        42,
        "orders-durable",
    )
    assert rows[3]["cause"] is None and "cannot decode" in rows[3]["problem"]


@pytest.mark.parametrize(
    ("flags", "sequences"),
    [
        (["--service", "billing"], [4]),
        (["--cause", "decode"], [7]),
        (["--cause", "delivery-limit"], [3, 10]),
        (["--original-subject", "events.order.*"], [3, 7, 10]),
        (["--since", "10m"], [10]),
        (["--service", "orders", "--since", "45m"], [7, 10]),
    ],
)
async def test_ls_applies_each_filter(flags, sequences):
    _, out, _ = await _run("ls", "--json", *flags)

    assert [json.loads(line)["sequence"] for line in out.splitlines()] == sequences


async def test_since_counts_back_from_the_clock_it_is_given_not_the_real_one():
    # Record 10 was stored 2 minutes before NOW. A clock an hour later puts it 62 minutes back.
    later = NOW + datetime.timedelta(hours=1)

    _, within, _ = await _run("ls", "--json", "--since", "10m", now=lambda: NOW)
    _, aged_out, _ = await _run("ls", "--json", "--since", "10m", now=lambda: later)
    _, kept, _ = await _run("ls", "--json", "--since", "3h", now=lambda: later)

    assert [json.loads(line)["sequence"] for line in within.splitlines()] == [10]
    assert aged_out == ""
    assert [json.loads(line)["sequence"] for line in kept.splitlines()] == [3, 4, 7, 8, 10]


@pytest.mark.parametrize("command", [["ls"], ["count"], ["show", "1"]])
async def test_a_named_stream_the_broker_does_not_hold_exits_4_not_a_traceback(command):
    code, out, err = await _run(*command, "--stream", "NOPE")

    assert code == 4 and out == ""
    assert "the broker holds no stream named 'NOPE'" in err


async def test_a_missing_message_in_a_stream_that_exists_is_still_exit_5():
    code, _, err = await _run("show", "999", "--stream", "DLQ")

    assert code == 5 and "holds no message 999" in err


@pytest.mark.parametrize("command", [["ls"], ["count"], ["show", "1"]])
async def test_a_wildcard_that_several_streams_hold_exits_4_and_reads_nothing(command):
    streams = _per_service_streams()

    code, out, err = await _run(*command, streams=streams)

    assert code == 4 and out == ""
    assert "3 streams hold the subject 'dlq.*': DLQ_A, DLQ_B, DLQ_C" in err
    assert {call[0] for call in streams.calls} == {"names"}


async def test_naming_one_of_several_streams_reads_that_one_only():
    code, out, _ = await _run(
        "count", "--stream", "DLQ_A", "--subject", "dlq.orders", streams=_per_service_streams()
    )

    assert code == 0 and out.splitlines()[-1].split() == ["total", "2"]


async def test_ls_with_a_subject_the_stream_does_not_hold_exits_4():
    code, out, err = await _run("ls", "--subject", "staging.dlq.*")

    assert code == 4 and out == "" and "no stream holds the subject" in err


async def test_ls_of_an_empty_stream_prints_nothing_and_exits_0():
    code, out, _ = await _run("ls", streams=FakeStreams([]))

    assert (code, out) == (0, "")


async def test_show_prints_the_record_its_headers_and_how_to_read_the_original():
    code, out, _ = await _run("show", "3")

    assert code == 0
    assert "sequence: 3" in out and "cause:    delivery-limit" in out
    assert "Content-Type: application/json" in out
    assert '"error": "TypeError: unlucky"' in out
    assert "nats stream get EVENTS 42" in out


async def test_show_of_a_record_without_the_delivery_fields_gives_no_command():
    _, out, _ = await _run("show", "7")

    assert "nats stream get" not in out and "cause:    decode" in out


async def test_show_of_an_unreadable_message_says_why():
    code, out, _ = await _run("show", "8")

    assert code == 0 and "cause:    unreadable" in out and "problem:" in out


async def test_show_json_is_one_object_with_the_record_inside():
    _, out, _ = await _run("show", "4", "--json")

    shown = json.loads(out)
    assert shown["sequence"] == 4 and shown["record"]["schema"] == "Payment"
    assert shown["headers"] == {"Content-Type": "application/json"}


async def test_show_of_a_sequence_the_stream_does_not_hold_exits_5():
    code, out, err = await _run("show", "5")

    assert code == 5 and out == "" and "holds no message 5" in err


async def test_count_groups_by_service_and_cause_with_a_total():
    code, out, _ = await _run("count")

    assert code == 0
    rows = out.splitlines()
    assert rows[0].split() == ["service", "cause", "count"]
    assert rows[1].split() == ["orders", "delivery-limit", "2"]
    assert {tuple(r.split()) for r in rows[2:-1]} == {
        ("billing", "invalid", "1"),
        ("orders", "decode", "1"),
        ("-", "unreadable", "1"),
    }
    assert rows[-1].split() == ["total", "5"]


async def test_count_json_and_filters():
    _, out, _ = await _run("count", "--json", "--service", "orders")

    counted = json.loads(out)
    assert counted["total"] == 3
    assert {(c["cause"], c["count"]) for c in counted["counts"]} == {
        ("delivery-limit", 2),
        ("decode", 1),
    }


async def test_count_of_nothing_is_a_total_of_zero_and_exit_0():
    code, out, _ = await _run("count", "--service", "nobody")

    assert code == 0 and out.splitlines()[-1].split() == ["total", "0"]


def test_a_bad_duration_is_a_usage_error_with_exit_7(capsys):
    with pytest.raises(SystemExit) as exit_:
        cli.main(["ls", "--since", "banana"])

    assert exit_.value.code == 7
    assert "--since" in capsys.readouterr().err


@pytest.mark.parametrize(
    "argv",
    [
        [],
        ["frobnicate"],
        ["ls", "--nope"],
        ["show"],
        ["show", "abc"],
        ["ls", "--cause", "other"],
        ["ls", "--limit", "0"],
    ],
)
def test_a_wrong_command_line_exits_7(argv):
    with pytest.raises(SystemExit) as exit_:
        cli.main(argv)

    assert exit_.value.code == 7


def test_no_broker_exits_3_and_the_message_does_not_carry_the_url_password(capsys):
    code = cli.main(["count", "--server", "nats://operator:hunter2@127.0.0.1:1", "--timeout", "1"])

    err = capsys.readouterr().err
    assert code == 3 and "no broker answered at 127.0.0.1:1" in err
    assert "hunter2" not in err


class FakeConnection:
    """A connection that answers one request, recording what it was asked."""

    def __init__(self, body):
        self.body = body
        self.asked: list[tuple] = []

    async def request(self, subject, payload=b"", timeout=None):
        self.asked.append((subject, json.loads(payload), timeout))
        return SimpleNamespace(data=json.dumps(self.body).encode())


async def test_the_stream_names_are_asked_for_with_one_request_that_carries_the_subject():
    connection = FakeConnection({"streams": ["DLQX_A", "DLQX_B"], "total": 2})

    names = await stream_names(connection, "dlqx.*", 3.0)

    assert names == ["DLQX_A", "DLQX_B"]
    assert connection.asked == [("$JS.API.STREAM.NAMES", {"subject": "dlqx.*"}, 3.0)]


async def test_no_streams_is_an_empty_list_not_an_error():
    assert await stream_names(FakeConnection({"streams": None, "total": 0}), "x.*", 1.0) == []


@pytest.mark.parametrize(
    ("error", "raised"),
    [
        ({"code": 404, "err_code": 10059, "description": "stream not found"}, NotFoundError),
        (
            {"code": 503, "err_code": 10039, "description": "jetstream not enabled"},
            ServiceUnavailableError,
        ),
    ],
)
async def test_an_error_body_from_the_broker_is_raised_as_nats_pys_error(error, raised):
    with pytest.raises(raised):
        await stream_names(FakeConnection({"error": error}), "x.*", 1.0)
