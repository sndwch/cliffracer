"""An idempotency key is the same in every process, or there is no key.

`compute_payload_hash` promised "a deterministic hash" and delivered one only
for what JSON could already encode. Everything else went through
`default=str`, and then through `except Exception: str(data)` -- so an ordinary
object hashed its `repr()`, which carries a memory address.

The failure was silent and was exactly what the feature prevents: a retry from
a restarted process computed a different key, JetStream saw an id it had not
seen, and stored the duplicate. Nothing raised.

MEASURED ACROSS PROCESSES, not within one. Two of the three defects only appear
between processes -- `PYTHONHASHSEED` differs per process by default, and a
memory address differs per run -- so a single-process test would have passed on
the broken version. These spawn subprocesses with different seeds.
"""

from __future__ import annotations

import dataclasses
import datetime
import decimal
import enum
import json
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, computed_field, field_serializer, model_serializer
from pydantic_core import PydanticSerializationError

import cliffracer
from cliffracer.core.exceptions import IdempotencyKeyError
from cliffracer.core.idempotency import compute_payload_hash

pytestmark = pytest.mark.unit

REPO = Path(__file__).resolve().parents[2]


class _Colour(enum.Enum):
    RED = "red"


class _Shade(enum.StrEnum):
    DARK = "dark"


@dataclasses.dataclass
class _Line:
    sku: str
    qty: int


#: The `src` directory this process imported `cliffracer` from. A child is pointed at it, so it hashes
#: with the code under test: without it the child imports whatever the interpreter has installed,
#: which in a scratch copy of the tree is another tree's code.
IMPORTED_SRC = str(Path(cliffracer.__file__).resolve().parents[1])


def _hash_in_subprocess(source: str, seed: str) -> tuple[int, str]:
    """Run `source` under a given PYTHONHASHSEED and return (returncode, stdout+stderr)."""
    proc = subprocess.run(
        [sys.executable, "-c", source],
        capture_output=True,
        text=True,
        cwd=str(REPO),
        env={"PYTHONHASHSEED": seed, "PATH": "/usr/bin:/bin", "PYTHONPATH": IMPORTED_SRC},
    )
    return proc.returncode, proc.stdout.strip() + proc.stderr.strip()


def test_a_child_process_hashes_with_the_code_this_process_imported():
    """Every cross-process row compares children, so each must run the code under test."""
    rc, out = _hash_in_subprocess("import cliffracer; print(cliffracer.__file__)", "1")

    assert rc == 0, out
    assert Path(out).resolve().parents[1] == Path(IMPORTED_SRC), (out, IMPORTED_SRC)


SET_PAYLOAD = """
from cliffracer.core.idempotency import compute_payload_hash
print(compute_payload_hash({"tags": {"a", "b", "c", "d", "e", "f", "g"}}))
"""

OBJECT_PAYLOAD = """
from cliffracer.core.idempotency import compute_payload_hash
class Order:
    def __init__(self, oid):
        self.id = oid
try:
    print(compute_payload_hash({"order": Order("abc")}))
except Exception as exc:
    print(type(exc).__name__)
"""


# --- the two cross-process defects ------------------------------------------


def test_a_set_hashes_the_same_in_two_processes():
    """Set iteration order follows PYTHONHASHSEED, so this failed before.

    Seven members because the ordering of a small set can coincide between two
    seeds; with seven it does not, which is why the issue's own reproduction
    used that many.
    """
    first_rc, first = _hash_in_subprocess(SET_PAYLOAD, "1")
    second_rc, second = _hash_in_subprocess(SET_PAYLOAD, "2")

    assert first_rc == 0, first
    assert second_rc == 0, second
    assert first == second, f"a set hashed {first} under one seed and {second} under another"


def test_an_unencodable_object_raises_the_same_named_error_in_two_processes():
    """The object case cannot be encoded, so it must refuse -- identically.

    Refusing is the decision the docstring forces: it promises determinism, and
    there is no deterministic encoding of an arbitrary object. Answering with
    its `repr()` produced a key that looked fine and deduplicated nothing.
    """
    first_rc, first = _hash_in_subprocess(OBJECT_PAYLOAD, "1")
    second_rc, second = _hash_in_subprocess(OBJECT_PAYLOAD, "2")

    assert first_rc == 0 and second_rc == 0, (first, second)
    assert first == second == "IdempotencyKeyError", (first, second)


#: `compute_payload_hash` of the order in `test_a_model_holding_no_set_keeps_its_key`, recorded on
#: the code before sets a model holds were put in order.
ORDER_VECTOR = "da0f322141969ac6abc6d54361bb151ef248f39eabeddc886c7df6de18f4991a"

MODEL_SET_PAYLOADS = {
    "a model's set of str": "Tagged(tags=TAGS)",
    "a model's frozenset of str": "Frozen(tags=frozenset(TAGS))",
    "a model in a dict payload": '{"order": Tagged(tags=TAGS)}',
    "a model in a list in a dict payload": '{"orders": [Tagged(tags=TAGS)]}',
    "a model in a model": "Outer(inner=Tagged(tags=TAGS))",
    "a set of str in a model's list": 'Listed(groups=[TAGS, {"x", "y", "z", "w"}])',
    "a model's set of int": "Counted(ids={10, 3, 2**40, -1, 7})",
    "a model that dumps a set by its alias": "Aliased(Tags=TAGS)",
    "a computed set of str": "Computed()",
    "a computed frozenset of str": "ComputedFrozen()",
    "a computed model holding a set": "ComputedModel()",
    "a computed list of sets": "ComputedGroups()",
    "a computed set with an alias": "ComputedAliased()",
    "an extra value holding a set": "Extra(n=1, tags=TAGS)",
    "a root model of a set": "Root(TAGS)",
    "a dataclass field holding a set": "HoldsDataclass(held=Line(tags=TAGS))",
    "a pydantic dataclass field holding a set": "HoldsPydanticDataclass(held=PydanticLine(tags=TAGS))",
    "a typed dict field holding a set": 'HoldsTypedDict(held={"tags": TAGS})',
}

MODEL_SET_SOURCE = """
import dataclasses
from typing import TypedDict
from pydantic import BaseModel, ConfigDict, Field, RootModel, computed_field
from pydantic.dataclasses import dataclass as pydantic_dataclass
from cliffracer.core.idempotency import compute_payload_hash
TAGS = {"a", "b", "c", "d", "e", "f", "g"}
class Tagged(BaseModel):
    tags: set[str]
class Frozen(BaseModel):
    tags: frozenset[str]
class Outer(BaseModel):
    inner: Tagged
class Listed(BaseModel):
    groups: list[set[str]]
class Counted(BaseModel):
    ids: set[int]
class Aliased(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    tags: set[str] = Field(alias="Tags")
class Computed(BaseModel):
    n: int = 0
    @computed_field
    @property
    def tags(self) -> set[str]:
        return set(TAGS)
class ComputedFrozen(BaseModel):
    @computed_field
    @property
    def tags(self) -> frozenset[str]:
        return frozenset(TAGS)
class ComputedModel(BaseModel):
    @computed_field
    @property
    def inner(self) -> Tagged:
        return Tagged(tags=TAGS)
class ComputedGroups(BaseModel):
    @computed_field
    @property
    def groups(self) -> list[set[str]]:
        return [set(TAGS), {"x", "y", "z", "w"}]
class ComputedAliased(BaseModel):
    model_config = ConfigDict(serialize_by_alias=True)
    @computed_field(alias="Tags")
    @property
    def tags(self) -> set[str]:
        return set(TAGS)
class Extra(BaseModel):
    model_config = ConfigDict(extra="allow")
    n: int = 0
class Root(RootModel[set[str]]):
    pass
@dataclasses.dataclass
class Line:
    tags: set[str]
class HoldsDataclass(BaseModel):
    held: Line
@pydantic_dataclass
class PydanticLine:
    tags: set[str]
class HoldsPydanticDataclass(BaseModel):
    held: PydanticLine
class Tags(TypedDict):
    tags: set[str]
class HoldsTypedDict(BaseModel):
    held: Tags
print(compute_payload_hash(PAYLOAD))
"""


@pytest.mark.parametrize("payload", MODEL_SET_PAYLOADS.values(), ids=MODEL_SET_PAYLOADS.keys())
def test_a_set_a_model_holds_hashes_the_same_in_three_processes(payload):
    """A model is hashed through its JSON dump, where a set is already a list in the order it
    iterated; each set is put in order by its members before it is hashed, as a set given
    directly is."""
    source = MODEL_SET_SOURCE.replace("PAYLOAD", payload)
    runs = [_hash_in_subprocess(source, seed) for seed in ("1", "2", "3")]

    assert all(rc == 0 for rc, _ in runs), runs
    assert len({out for _, out in runs}) == 1, f"{payload} hashed differently per seed: {runs}"


def test_a_models_set_hashes_as_the_set_it_holds():
    """One encoding for a set, whether a model holds it or a dict does."""
    from pydantic import BaseModel

    class Counted(BaseModel):
        ids: set[int]

    assert compute_payload_hash(Counted(ids={3, 1, 2})) == compute_payload_hash({"ids": {1, 2, 3}})


def test_a_models_list_keeps_its_order():
    """Only what the model holds as a set is put in order: a list is a sequence."""
    from pydantic import BaseModel

    class Listed(BaseModel):
        tags: list[str]
        pairs: list[tuple[str, str]]

    assert compute_payload_hash(Listed(tags=["b", "a"], pairs=[])) != compute_payload_hash(
        Listed(tags=["a", "b"], pairs=[])
    )
    assert compute_payload_hash(Listed(tags=[], pairs=[("b", "a")])) != compute_payload_hash(
        Listed(tags=[], pairs=[("a", "b")])
    )


def test_a_model_holding_no_set_keeps_its_key():
    """A vector recorded from the code before sets were put in order: a model with no set in
    it, a nested model, a list, a datetime and an enum among its fields, keeps its key."""
    from pydantic import BaseModel

    class Inner(BaseModel):
        sku: str
        at: datetime.datetime

    class Order(BaseModel):
        id: str
        lines: list[Inner]
        colour: _Colour
        note: str | None = None

    order = Order(
        id="abc",
        lines=[Inner(sku="b", at=datetime.datetime(2026, 1, 2, 3, 4, 5))],
        colour=_Colour.RED,
    )

    assert compute_payload_hash(order) == ORDER_VECTOR


# --- the third defect, which is visible inside one process ------------------


def test_two_equal_dicts_hash_equal_whatever_order_they_were_built_in():
    """`sort_keys=True` raises on mixed keys, and the old fallback kept insertion order.

    So `{1: "a", "b": 2}` and `{"b": 2, 1: "a"}` -- equal dicts -- produced
    different keys, which is the same defect as the others without needing a
    second process to see it.
    """
    first = {1: "a", "b": 2}
    second = {"b": 2, 1: "a"}

    assert first == second, "the premise: these are the same payload"
    assert compute_payload_hash(first) == compute_payload_hash(second)


def test_a_key_that_is_a_number_does_not_collide_with_its_string():
    """The encoding must not make two different payloads the same one.

    Encoding non-string keys to strings would map `{1: "x"}` and `{"1": "x"}`
    onto one key, which is a worse failure than the one being fixed: a
    collision deduplicates two publishes that are not duplicates.
    """
    assert compute_payload_hash({1: "x", 2: "y"}) != compute_payload_hash({"1": "x", "2": "y"})


# --- what must NOT change ---------------------------------------------------


@pytest.mark.parametrize(
    ("payload", "expected"),
    [
        (
            {"a": 1, "b": "two", "c": [1, 2, 3]},
            "81b1e3981153aa1262ce623c480b1065ca92dc47f792c8403eb9004e370a1eb0",
        ),
        (
            {"at": datetime.datetime(2026, 1, 1, 12, 0, 0)},
            "4f9af07292a1e258c2016a1ce5671ce3363f88032b89f5fe11d8f5f9ea72a2b9",
        ),
        (
            {"id": uuid.UUID(int=1)},
            "2dc5f9c55037c706dbbeee318933924c80a78eeff51d5f0d9310ced9801bfe6b",
        ),
        (
            {"amt": decimal.Decimal("1.50")},
            "51ac7f5e778ca8d80d107fd7478e7372ec7c306cd7255d56f865e8645c455cc8",
        ),
        (
            {"b": b"\x00\xff"},
            "6f87a4df75d4ac8e6a9b33ba3149ed5c043d63ecf5d0b8b58fc1d06f529f10de",
        ),
        (
            {"c": _Colour.RED},
            "cd14656db5521b8ca055d0898184f0d1824ba8ae3f392781e093b4e67878bb5e",
        ),
        (
            {"c": _Shade.DARK},
            "2b9b40d269a922de7436cd92ddf2f330151352a0831f2b870ff8379440e5301e",
        ),
        (
            {"t": (1, "a")},
            "a38414fbac800efd0e34bf23f5a11c6c6a8e9ed29a191de25e23491865c42e39",
        ),
    ],
    ids=["plain json", "datetime", "uuid", "decimal", "bytes", "enum", "str enum", "tuple"],
    # The enum vector is tied to `_Colour`'s NAME, because `str()` of a plain
    # member is "Class.NAME". That was true before this change too, so it is
    # faithful rather than a new coupling -- but renaming the class here moves
    # the vector, which is why the class is defined at module scope and named
    # deliberately.
)
def test_a_payload_that_already_hashed_correctly_keeps_its_key(payload, expected):
    """Fixed vectors recorded from the code BEFORE this change.

    These four were already deterministic -- `default=str` is a property of the
    value for a datetime, a UUID and a Decimal, and plain JSON never reached
    the fallback at all. Moving them would silently re-key every in-flight
    publish across a deploy, which is a worse outage than the bug: a service
    would stop recognising its own retries on the version that fixed
    recognising them.

    So the encoder emits a plain object for string-keyed dicts and keeps
    `str()` for those types, and these values pin that.
    """
    assert compute_payload_hash(payload) == expected


def test_a_model_and_the_dict_it_dumps_to_still_agree():
    """Pydantic models were already handled and must stay on the same key."""
    from pydantic import BaseModel

    class Order(BaseModel):
        id: str
        qty: int

    order = Order(id="abc", qty=2)
    assert compute_payload_hash(order) == compute_payload_hash({"id": "abc", "qty": 2})


# --- what the refusal says --------------------------------------------------


def test_the_refusal_names_the_type_and_where_it_was():
    """A key that cannot be computed has to be actionable, not just loud."""

    class Order:
        pass

    with pytest.raises(IdempotencyKeyError) as caught:
        compute_payload_hash({"outer": {"order": Order()}})

    message = str(caught.value)
    assert "Order" in message, message
    assert "'order'" in message, message
    assert "key=" in message, "the message should say what to do instead"


class _Opaque:
    pass


class _Blob(BaseModel):
    note: str = "n"
    blob: bytes


class _RaisingProperty(BaseModel):
    n: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def boom(self) -> int:
        raise RuntimeError("the property failed")


class _HoldsAnything(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    thing: Any


class _HoldsABlob(BaseModel):
    inner: _Blob


class _BlobsInAList(BaseModel):
    title: str = "t"
    blobs: list[_Blob]


class _BlobsByKey(BaseModel):
    by: dict[str, _Blob]


class _BlobInAPair(BaseModel):
    pair: tuple[str, _Blob]


class _BlobsInNestedLists(BaseModel):
    rows: list[list[_Blob]]


class _ModelSerializerRaises(BaseModel):
    first: int = 1
    second: int = 2

    @model_serializer
    def _whole(self) -> dict[str, int]:
        raise RuntimeError("the model serializer failed")


class _OneFieldModelSerializerRaises(BaseModel):
    only: int = 1

    @model_serializer
    def _whole(self) -> dict[str, int]:
        raise RuntimeError("the model serializer failed")


class _WrapsAndHandsOn(BaseModel):
    inner: _Blob

    @model_serializer(mode="wrap")
    def _wrap(self, handler: Any) -> Any:
        return handler(self)


class _TwoBadFields(BaseModel):
    first: bytes
    second: bytes


class _FieldSerializerRaises(BaseModel):
    first: int = 1
    second: int = 2

    @field_serializer("second")
    def _second(self, value: int) -> int:
        raise RuntimeError("the field serializer failed")


@pytest.mark.parametrize(
    ("make", "model", "where", "cause"),
    [
        pytest.param(
            lambda: _Blob(blob=b"\x00\xff"),
            "_Blob",
            "payload['blob']",
            UnicodeDecodeError,
            id="bytes-that-are-not-utf8",
        ),
        pytest.param(
            lambda: _RaisingProperty(),
            "_RaisingProperty",
            "payload['boom']",
            RuntimeError,
            id="a-computed-field-that-raises",
        ),
        pytest.param(
            lambda: _HoldsAnything(thing=_Opaque()),
            "_HoldsAnything",
            "payload['thing']",
            PydanticSerializationError,
            id="an-object-json-cannot-write",
        ),
        pytest.param(
            lambda: _HoldsABlob(inner=_Blob(blob=b"\xff")),
            "_Blob",
            "payload['inner']['blob']",
            UnicodeDecodeError,
            id="in-a-nested-model",
        ),
        pytest.param(
            lambda: {"order": _Blob(blob=b"\xff")},
            "_Blob",
            "payload['order']['blob']",
            UnicodeDecodeError,
            id="a-model-in-a-dict-payload",
        ),
        pytest.param(
            lambda: {"orders": [_Blob(blob=b"\xff")]},
            "_Blob",
            "payload['orders'][0]['blob']",
            UnicodeDecodeError,
            id="a-model-deeper-in-a-dict-payload",
        ),
        pytest.param(
            lambda: _BlobsInAList(blobs=[_Blob(blob=b"ok"), _Blob(blob=b"\xff")]),
            "_Blob",
            "payload['blobs'][1]['blob']",
            UnicodeDecodeError,
            id="a-model-in-a-list-field",
        ),
        pytest.param(
            lambda: _BlobsByKey(by={"k": _Blob(blob=b"\xff")}),
            "_Blob",
            "payload['by']['k']['blob']",
            UnicodeDecodeError,
            id="a-model-in-a-dict-field",
        ),
        pytest.param(
            lambda: _BlobInAPair(pair=("a", _Blob(blob=b"\xff"))),
            "_Blob",
            "payload['pair'][1]['blob']",
            UnicodeDecodeError,
            id="a-model-in-a-tuple-field",
        ),
        pytest.param(
            lambda: _BlobsInNestedLists(rows=[[], [_Blob(blob=b"ok"), _Blob(blob=b"\xff")]]),
            "_Blob",
            "payload['rows'][1][1]['blob']",
            UnicodeDecodeError,
            id="a-model-in-nested-lists",
        ),
        pytest.param(
            lambda: _ModelSerializerRaises(),
            "_ModelSerializerRaises",
            "payload of _ModelSerializerRaises",
            PydanticSerializationError,
            id="a-model-serializer-that-raises",
        ),
        pytest.param(
            lambda: _OneFieldModelSerializerRaises(),
            "_OneFieldModelSerializerRaises",
            "payload of _OneFieldModelSerializerRaises",
            PydanticSerializationError,
            id="a-model-serializer-on-a-one-field-model",
        ),
        pytest.param(
            lambda: _WrapsAndHandsOn(inner=_Blob(blob=b"\xff")),
            "_Blob",
            "payload['inner']['blob'] of _Blob",
            PydanticSerializationError,
            id="a-wrap-serializer-that-hands-the-dump-on",
        ),
        pytest.param(
            lambda: _TwoBadFields(first=b"\xff", second=b"\xfe"),
            "_TwoBadFields",
            "payload['first'] of _TwoBadFields",
            UnicodeDecodeError,
            id="two-fields-that-each-fail",
        ),
        pytest.param(
            lambda: _FieldSerializerRaises(),
            "_FieldSerializerRaises",
            "payload['second'] of _FieldSerializerRaises",
            PydanticSerializationError,
            id="a-field-serializer-that-raises",
        ),
    ],
)
def test_a_model_that_cannot_be_written_as_json_is_refused_by_name(make, model, where, cause):
    """A model is hashed through its JSON dump. A dump that fails is refused as any value that
    cannot be hashed is: `IdempotencyKeyError`, naming the model and the field, with the dump's
    own error as its cause and none of the value in the message."""
    with pytest.raises(IdempotencyKeyError) as caught:
        compute_payload_hash(make())

    message = str(caught.value)
    assert model in message and where in message, message
    assert "key=" in message, "the message should say what to do instead"
    assert isinstance(caught.value.__cause__, cause), repr(caught.value.__cause__)
    assert "\\xff" not in message, message
    assert "the property failed" not in message and "serializer failed" not in message, message


def test_a_model_holding_bytes_that_are_utf8_still_hashes():
    """The refusal is for a dump that fails, not for every bytes field."""
    assert compute_payload_hash(_Blob(blob=b"abc")) == compute_payload_hash(
        {"note": "n", "blob": "abc"}
    )


def test_a_dataclass_is_encodable_rather_than_refused():
    """Refusing is for what cannot be encoded, not for everything non-JSON."""
    import dataclasses

    @dataclasses.dataclass
    class Line:
        sku: str
        qty: int

    assert compute_payload_hash({"line": Line("a", 1)}) == compute_payload_hash(
        {"line": {"sku": "a", "qty": 1}}
    )


def test_bytes_and_enums_keep_the_encoding_they_had():
    """They were already stable, so this change does not re-encode them.

    This began as `== compute_payload_hash({"b": "00ff"})` -- asserting that
    bytes ENCODE, not what they encode TO -- which is exactly the shape that
    let an earlier version of this change re-key every payload carrying a blob
    with nothing going red. Found by comparing the two encodings per type
    rather than by running the tests.
    """
    assert compute_payload_hash({"b": b"\x00\xff"}) == compute_payload_hash({"b": str(b"\x00\xff")})
    assert compute_payload_hash({"c": _Colour.RED}) == compute_payload_hash({"c": "_Colour.RED"})


def test_a_set_and_a_list_of_the_same_members_are_different_payloads():
    """The set marker is not decoration: a set is not the list it sorts to."""
    assert compute_payload_hash({"x": {1, 2}}) != compute_payload_hash({"x": [1, 2]})


def test_no_json_dumps_in_this_module_carries_a_default_hook():
    """The mechanism, asserted directly.

    `default=` is what turned an object into its `repr()`, so a reintroduction
    is worth catching structurally rather than waiting for a payload nobody
    parameterised.

    READ AS CODE, NOT AS TEXT. The first version of this scanned the source for
    the string `default=str` and failed on this module's own comments, which
    explain the defect and therefore contain it. A guard against a construct
    has to be able to tell the construct from prose about it, and the parse
    tree is what does that.
    """
    import ast

    module = ast.parse((REPO / "src" / "cliffracer" / "core" / "idempotency.py").read_text())
    offenders = [
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dumps"
        and any(kw.arg == "default" for kw in node.keywords)
    ]

    assert not offenders, f"json.dumps(..., default=...) is back at line(s) {offenders}"


def test_CONTROL_that_scan_finds_a_default_hook_when_there_is_one():
    """Otherwise the test above passes on a module it failed to parse."""
    import ast

    module = ast.parse("import json\njson.dumps({}, default=str)\n")
    found = [
        node.lineno
        for node in ast.walk(module)
        if isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr == "dumps"
        and any(kw.arg == "default" for kw in node.keywords)
    ]

    assert found == [2], found


def test_the_canonical_form_is_json_and_stays_parseable():
    """A sanity check on the encoder itself, independent of any hash."""
    from cliffracer.core.idempotency import _canonical_json

    encoded = _canonical_json({"b": 1, "a": {2, 1}})
    assert json.loads(encoded) == {"a": {"__set__": [1, 2]}, "b": 1}
    assert encoded.index('"a"') < encoded.index('"b"'), "keys are not in canonical order"


# --- what this change deliberately re-keys ----------------------------------


@pytest.mark.parametrize(
    ("payload", "why"),
    [
        (
            {"l": _Line("a", 1)},
            "str() of a dataclass interpolates each field's repr, so it is stable only "
            "when every field is; a dataclass holding an object encoded its address",
        ),
        (
            {"s": {1, 2, 3}},
            "set stability is a property of the MEMBERS -- str and tuple members vary "
            "with PYTHONHASHSEED, int and float members do not -- so one encoding for "
            "set means numeric sets move",
        ),
        ({1: "a", "b": 2}, "these hashed by insertion order, so equal dicts had two keys"),
    ],
    ids=["dataclass", "numeric set", "mixed-key dict"],
)
def test_these_keys_move_and_that_is_deliberate(payload, why):
    """Recorded so the re-key is a decision in the tree, not a surprise in production.

    The first two were already deterministic for the values tested here, which
    is exactly why they need writing down: measured per TYPE they look safe,
    and the property is per VALUE.

    Only that a key is produced. Pinning the new values would be a second set
    of fixed vectors freezing an encoding that has not earned it; the vectors
    above pin what must NOT move, which is the direction that costs an outage.
    """
    assert compute_payload_hash(payload)
    assert why


def test_a_dataclass_holding_an_object_is_refused_rather_than_keyed_on_its_address():
    """Why the dataclass encoding cannot go back to `str()`.

    Pre-fix this produced `Holder(thing=<Opaque object at 0x7f...>)` and hashed
    the address. Keeping `str()` to avoid re-keying simple dataclasses would
    have kept the original defect for the shape that carries it -- so the
    dataclass re-key is not a preference, it is the fix.
    """

    class Opaque:
        pass

    @dataclasses.dataclass
    class Holder:
        thing: object

    with pytest.raises(IdempotencyKeyError):
        compute_payload_hash({"h": Holder(Opaque())})
