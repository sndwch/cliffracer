"""`pack_msgpack` reuses one `msgpack.Packer` for each thread and packs the bytes it always packed.

`msgpack.packb` builds a packer for every call, which is about a fifth of a 1 KB call. A packer keeps
an internal buffer, so one shared by two threads could mix their output wherever a pack yields (a
free-threaded interpreter, or a `default` hook); there is one for each thread, built on first use.
A pack that holds the GIL throughout cannot be interrupted, so the concurrent test below pins the
behaviour on any build and the identity test pins that no packer is shared. The bytes are the point: the output is held byte for byte to
`msgpack.packb(to_jsonable_python(value), use_bin_type=True)`, the body before the change, for every
shape the type-domain tests cover and for a payload that is refused.
"""

import threading
from concurrent.futures import ThreadPoolExecutor

import msgpack
import pydantic_core
import pytest

from cliffracer.core import validation
from cliffracer.core.validation import pack_msgpack
from tests.unit.test_json_and_msgpack_carry_one_type_domain import SHAPES

pytestmark = pytest.mark.unit


def packed_as_before(value) -> bytes:
    return msgpack.packb(pydantic_core.to_jsonable_python(value), use_bin_type=True)


EXTRA_SHAPES = {
    "empty containers": {"a": [], "b": {}, "c": ""},
    "unicode": {"k": "café ☃ \U0001f600"},
    "large integers": {"small": -(2**31), "big": 2**63 - 1, "unsigned": 2**64 - 1},
    "floats": {"pi": 3.141592653589793, "tiny": 5e-324, "neg": -0.0},
    "nested lists and none": [None, True, False, [1, [2, [3]]], {"x": None}],
    "strings across the str8 and str16 boundaries": {"a": "y" * 40, "b": "z" * 300},
    "a long string": {"s": "x" * 70_000},
    "a scalar": 7,
}


@pytest.mark.parametrize(
    "value",
    [*SHAPES.values(), *EXTRA_SHAPES.values()],
    ids=[*SHAPES.keys(), *EXTRA_SHAPES.keys()],
)
def test_the_bytes_are_those_the_body_before_the_change_produced(value):
    assert pack_msgpack(value) == packed_as_before(value)


def test_the_same_payload_packed_twice_gives_the_same_bytes():
    payload = {"id": 7, "tags": ["a", "b"], "blob": b"abc"}

    assert pack_msgpack(payload) == pack_msgpack(payload) == packed_as_before(payload)


def test_a_payload_that_is_refused_is_refused_as_before_and_leaves_nothing_in_the_packer():
    for refused in ({"blob": b"\xff\xfe"}, {"x": object()}):
        with pytest.raises(Exception) as before:
            packed_as_before(refused)
        with pytest.raises(type(before.value)):
            pack_msgpack(refused)
        # The packer that raised is the one the next call uses; none of the failed pack is left in it.
        assert pack_msgpack({"after": 1}) == packed_as_before({"after": 1})


def test_a_thread_reuses_its_packer_and_two_threads_do_not_share_one():
    mine = (validation._packer(), validation._packer())
    seen: list[object] = []

    def other() -> None:
        seen.extend([validation._packer(), validation._packer()])

    thread = threading.Thread(target=other)
    thread.start()
    thread.join()

    assert mine[0] is mine[1]
    assert seen[0] is seen[1]
    assert seen[0] is not mine[0]


def test_two_threads_packing_different_payloads_at_once_each_get_their_own_bytes():
    """Each result equals `packb`, byte for byte, with the threads released together and repeated."""
    payloads = [
        {"who": "a", "rows": [{"n": i, "s": "a" * (i % 50)} for i in range(200)]},
        {"who": "b", "rows": [{"n": -i, "s": "b" * (i % 70), "blob": b"x"} for i in range(250)]},
    ]
    expected = [packed_as_before(p) for p in payloads]
    rounds = 300
    barrier = threading.Barrier(len(payloads))
    wrong: list[tuple[int, int]] = []

    def run(index: int) -> None:
        for round_ in range(rounds):
            barrier.wait()
            if pack_msgpack(payloads[index]) != expected[index]:
                wrong.append((index, round_))

    with ThreadPoolExecutor(max_workers=len(payloads)) as pool:
        list(pool.map(run, range(len(payloads))))

    assert wrong == []


def test_CONTROL_one_packer_shared_by_two_threads_does_mix_their_output():
    """Instrument control: it calls no code under test. A packer is shared state, and a pack that
    yields mixes two threads; this shows the mixing that
    `test_a_thread_reuses_its_packer_and_two_threads_do_not_share_one` and
    `test_two_threads_packing_different_payloads_at_once_each_get_their_own_bytes` look for is real.

    A pack that holds the GIL throughout is not interrupted, so the packer here has a `default`
    hook that sleeps (and so releases it) part way through, which is what a free-threaded build does
    anywhere. The shared packer then returns bytes that are not either payload's.
    """

    def slow(obj):
        threading.Event().wait(0.002)
        return str(obj)

    shared = msgpack.Packer(use_bin_type=True, default=slow)
    payloads = [{"who": "a", "x": object(), "y": [1, 2, 3]}, {"who": "b", "x": object(), "y": [4]}]
    barrier = threading.Barrier(2)
    results: list[bytes] = []
    lock = threading.Lock()

    def run(index: int) -> None:
        for _ in range(20):
            barrier.wait()
            try:
                result = shared.pack(payloads[index])
            except Exception:
                result = b""
            with lock:
                results.append(result)

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(run, range(2)))

    decoded = []
    for raw in results:
        try:
            value = msgpack.unpackb(raw, raw=False)
        except Exception:
            value = None
        decoded.append(value)
    clean = [v for v in decoded if isinstance(v, dict) and v.get("who") in {"a", "b"}]
    assert len(clean) < len(results), "a shared packer was expected to mix two threads' output"
