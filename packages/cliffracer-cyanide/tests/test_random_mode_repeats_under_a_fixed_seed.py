"""Random mode injects the same faults in a run that has the same seed, with or without caller ids.

The extension runs behind `CorrelationExtension`, which gives a message with no id of its own a
fresh one, so a draw keyed on `ctx.correlation_id` was different on every run for callers that send
none, and the seed the extension logs at start was not a way to replay a run. A draw is keyed on the
id the caller sent when there is one; otherwise on the subject, the payload and how many identical
messages came before.
"""

from typing import Any
from unittest.mock import MagicMock

import pytest
from cliffracer_cyanide import CyanideConfig, CyanideExtension

from cliffracer.core.correlation_extension import CorrelationExtension
from cliffracer.core.dispatch.pipeline import ExtensionPipeline
from cliffracer.core.extension import RejectMessage, WorkerContext

pytestmark = pytest.mark.unit

MESSAGES = 300
WEIGHTS = {
    "slow_weight": 0.2,
    "drop_reply_weight": 0.2,
    "raise_after_delay_weight": 0.2,
    "sleep_past_timeout_weight": 0.2,
}


def _extension(seed: str = "replay-seed", **overrides: Any):
    settings = {
        "enabled": True,
        "mode": "random",
        "seed": seed,
        "slow_delay": 0.0,
        "raise_delay": 0.0,
        "sleep_timeout_duration": 0.0,
        **WEIGHTS,
        **overrides,
    }
    ext = CyanideExtension(config=CyanideConfig(**settings))
    ext.name = "cyanide"
    correlation = CorrelationExtension()
    correlation.name = "_correlation"
    return ext, ExtensionPipeline([correlation, ext])


def _message(index: int, *, caller_id: str | None, distinct: bool) -> WorkerContext:
    headers = {"x-correlation-id": caller_id} if caller_id else {}
    return WorkerContext(
        kind="rpc",
        subject="chaos.rpc.work",
        headers=headers,
        correlation_id=None,
        payload={"n": index} if distinct else {"n": 0},
        raw=MagicMock(),
        data={"handler_name": "work"},
    )


async def _run_one(ext_and_pipeline, index: int) -> None:
    """One message with a distinct payload and no caller id."""
    _, pipeline = ext_and_pipeline

    async def call() -> None:
        return None

    try:
        await pipeline.run_worker(_message(index, caller_id=None, distinct=True), call)
    except RejectMessage:
        pass


async def _run(
    ext_and_pipeline,
    *,
    ids: list[str] | None = None,
    distinct: bool = False,
    count: int = MESSAGES,
) -> list[str]:
    """The mode each message was injected with, in dispatch order."""
    ext, pipeline = ext_and_pipeline

    async def call() -> None:
        return None

    for index in range(count):
        ctx = _message(index, caller_id=ids[index] if ids else None, distinct=distinct)
        try:
            await pipeline.run_worker(ctx, call)
        except RejectMessage:
            pass
    return [entry.mode for entry in ext.injections()]


async def test_messages_with_no_caller_id_replay_under_the_same_seed():
    first = await _run(_extension(), distinct=True)
    second = await _run(_extension(), distinct=True)

    assert first, "no fault was injected, so this asserts nothing"
    assert first == second


async def test_the_generated_ids_differ_between_the_runs_so_they_cannot_be_what_repeats():
    """The precondition: if the ids matched, the test above could not tell the old keying apart."""
    ext_a, ext_b = _extension(), _extension()
    await _run(ext_a, distinct=True)
    await _run(ext_b, distinct=True)

    ids_a = [entry.correlation_id for entry in ext_a[0].injections()]
    ids_b = [entry.correlation_id for entry in ext_b[0].injections()]

    assert ids_a and ids_a != ids_b


async def test_identical_messages_with_no_caller_id_get_a_mix_and_the_same_mix_each_run():
    """One subject and one payload, 300 times: neither all faulted nor none, and repeatable."""
    first = await _run(_extension(), distinct=False)
    second = await _run(_extension(), distinct=False)

    assert 0 < len(first) < MESSAGES, f"{len(first)} of {MESSAGES} were injected"
    assert len(set(first)) == 4
    assert first == second


async def test_a_caller_id_decides_the_draw_whatever_order_the_messages_arrive_in():
    ids = [f"req-{i}" for i in range(MESSAGES)]
    forward = _extension()
    await _run(forward, ids=ids, distinct=False)
    backward = _extension()
    await _run(backward, ids=list(reversed(ids)), distinct=False, count=MESSAGES)

    by_id_forward = {e.correlation_id: e.mode for e in forward[0].injections()}
    by_id_backward = {e.correlation_id: e.mode for e in backward[0].injections()}

    assert by_id_forward and by_id_forward == by_id_backward


async def test_distinct_payloads_with_no_caller_id_draw_the_same_whatever_order_they_arrive_in():
    """Without an id a message is its subject and payload, so reordering distinct ones changes nothing."""
    forward = _extension()
    reverse = _extension()
    modes: dict[str, dict[int, str | None]] = {"forward": {}, "reverse": {}}

    for label, ext, order in (
        ("forward", forward, range(MESSAGES)),
        ("reverse", reverse, reversed(range(MESSAGES))),
    ):
        for index in order:
            before = len(ext[0].injections())
            await _run_one(ext, index)
            after = ext[0].injections()
            modes[label][index] = after[-1].mode if len(after) > before else None

    assert any(modes["forward"].values()), "no fault was injected, so this asserts nothing"
    assert modes["forward"] == modes["reverse"]


async def test_CONTROL_a_different_seed_injects_different_faults():
    first = await _run(_extension("one"), distinct=True)
    other = await _run(_extension("two"), distinct=True)

    assert first != other


def _anonymous(subject: str, payload: dict[str, Any]) -> WorkerContext:
    return WorkerContext(
        kind="rpc", subject=subject, headers={}, correlation_id="generated", payload=payload
    )


def test_a_message_the_framework_named_is_not_taken_for_one_the_caller_named():
    """`ctx.correlation_id` is set in both cases; only an id the message itself carries counts."""
    ext, _ = _extension()
    ctx = WorkerContext(
        kind="rpc",
        subject="s",
        headers={"x-correlation-id": "sent"},
        correlation_id="corr_generated",
        payload={},
    )

    assert ext._caller_id(ctx) is None
    ctx.correlation_id = "sent"
    assert ext._caller_id(ctx) == "sent"
    payload_ctx = WorkerContext(
        kind="rpc", subject="s", headers={}, correlation_id="p-1", payload={"correlation_id": "p-1"}
    )
    assert ext._caller_id(payload_ctx) == "p-1"


def test_the_occurrence_table_is_bounded_and_a_forgotten_pair_starts_again():
    limit = 5
    bounded, _ = _extension(injection_record_limit=limit)
    fresh, _ = _extension(injection_record_limit=limit)

    first_draw = fresh._compute_random_mode(_anonymous("s", {"k": "x"}))
    second_draw = fresh._compute_random_mode(_anonymous("s", {"k": "x"}))
    assert first_draw != second_draw, (
        "the occurrences draw alike, so a forgotten count shows nothing"
    )
    bounded._compute_random_mode(_anonymous("s", {"k": "x"}))
    for index in range(limit + 3):
        bounded._compute_random_mode(_anonymous("s", {"other": index}))

    assert len(bounded._occurrences) == limit
    # "x" was pushed out, so it draws as a first occurrence again.
    assert bounded._compute_random_mode(_anonymous("s", {"k": "x"})) == first_draw


def test_a_pair_seen_again_is_the_last_to_be_forgotten():
    """Least recently SEEN goes first, not first seen: a pair that keeps arriving keeps its count."""
    limit = 5
    kept, _ = _extension(injection_record_limit=limit)
    fresh, _ = _extension(injection_record_limit=limit)
    ext_first, _ = _extension(injection_record_limit=limit)

    # What the third draw of "x" is, from an extension that has seen it twice before.
    for _ in range(2):
        fresh._compute_random_mode(_anonymous("s", {"k": "x"}))
    third = fresh._compute_random_mode(_anonymous("s", {"k": "x"}))
    first = ext_first._compute_random_mode(_anonymous("s", {"k": "x"}))
    assert third != first, (
        "the first and third occurrences draw alike, so a forgotten count shows nothing"
    )

    kept._compute_random_mode(_anonymous("s", {"k": "x"}))
    for index in range(limit - 1):
        kept._compute_random_mode(_anonymous("s", {"other": index}))
    kept._compute_random_mode(_anonymous("s", {"k": "x"}))  # seen again: now the newest
    for index in range(limit - 1):
        kept._compute_random_mode(_anonymous("s", {"later": index}))

    assert kept._compute_random_mode(_anonymous("s", {"k": "x"})) == third


def test_CONTROL_a_pair_still_remembered_draws_its_next_occurrence():
    ext, _ = _extension(injection_record_limit=50)
    draws = [ext._compute_random_mode(_anonymous("s", {"k": "x"})) for _ in range(40)]

    assert len(set(draws)) > 1, "forty occurrences of one message drew the same thing"
