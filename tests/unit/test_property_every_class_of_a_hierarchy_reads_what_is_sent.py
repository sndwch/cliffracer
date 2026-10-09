"""Every model class of a generated hierarchy reads what each send path sends, or a known limit says
why not.

A handler may declare any model class of a value's hierarchy, so for each generated case
(`tests.fixtures.properties.hierarchy`) every one of those classes reads what the wire
(`wire_models`), a generated client (`ServiceClient._encode`) and a KV write
(`serialize_value`, then `get(as_type=...)`) send for the leaf instance. Each reading is EQUAL or
REFUSED. A LOST or OTHER reading is allowed only under a named limit:

- H-W, the wire: what was sent is what the wire sends choosing among the dumps alone
  (`wire_models(value, extra_forms="none")`), the dumps it sent before the validation-alias form
  existed, so a handler declaring a base gets what it got before. `call_rpc` and its siblings do
  not know the handler's declared class.
- H-K1, a KV read by a base class: the base reads the stored bytes as it reads the form earlier
  releases stored (`_released_form`), so it reads what it read before.

The client path has no limit: `_encode` knows the declared class, so it sends what that class reads
back or refuses.

A refusal must not be needless, either: a REFUSED reading is a finding when the path could have
sent the value correctly. On the client, that is when the declared class reads one of the forms
the path considered back EQUAL, NaN equal to NaN (`hierarchy.offered_forms`: what the shipped
chooser is offered, and the plain dump the wire reads before it); on the wire, when every model
class of the hierarchy does, since the wire does not know which one the handler declares and refuses
rather than send a base other values. KV refusals have their own rule and are not judged here.
750 of the 2,500 cases are drawn with float fields: in those, each field other than `p` is a
`float` one time in four, holding 1.5, NaN, inf or -inf. Another 250 are drawn with dataclasses:
each field other than `p` is a `float`, a standard dataclass or a Pydantic dataclass one time in
four each, every float in it holding one of those values.
"""

from __future__ import annotations

import math
from typing import Any

import cliffracer_kv.serialization as kv
import pydantic_core
import pytest
from pydantic import AliasPath, BaseModel, Field

import cliffracer.core.validation as validation
from cliffracer.client import ServiceClient
from tests.fixtures.properties import (
    Finding,
    Limit,
    assert_control_finds,
    assert_matches_only,
    assert_only_known_limits,
    cases,
    false_send_shapes,
    judge,
    seeds,
)
from tests.fixtures.properties import hierarchy as H

pytestmark = pytest.mark.unit

FIXED_SEED = 7
CASES = 1500
#: Cases whose hierarchies also have float fields holding NaN, inf or -inf.
EXTRAS_CASES = 750
#: Cases whose hierarchies have standard and Pydantic dataclass fields holding them as well.
DATACLASS_CASES = 250
CONTROL_CASES = 300
#: A third of the uncovered findings the CONTROL makes over its cases on main (525 on seed 7;
#: 540 and 481 on seeds 20261003 and 424242).
CONTROL_FLOOR = 175

# H-K1 judges against the shipped function, even while a CONTROL replaces the KV write.
_released_form = kv._released_form


def _base_reads_kv_as_released(finding: Finding) -> bool:
    cell, inst = finding.detail
    if cell.path != "kv" or cell.outcome not in ("LOST", "OTHER") or cell.declared is type(inst):
        return False
    forms = {
        "field names": inst.model_dump_json(),
        "aliases": inst.model_dump_json(by_alias=True),
    }
    return _read(cell.declared, cell.sent) == _read(cell.declared, _released_form(inst, forms))


def _read(declared: type[BaseModel], text: Any) -> Any:
    try:
        back = declared.model_validate_json(text)
    except Exception:
        return "refused"
    return {name: getattr(back, name) for name in declared.model_fields}


LIMITS = [
    H.H_W,
    Limit(
        "H-K1",
        "a base class reads the stored bytes as it reads the form earlier releases stored",
        _base_reads_kv_as_released,
    ),
]


def findings(
    seed: int, count: int, extras: bool = False, with_dataclasses: bool = False
) -> list[Finding]:
    client = H._client()
    found = []
    stream = " (dataclasses)" if with_dataclasses else " (extras)" if extras else ""
    for index in range(count):
        case = H.build(seed, index, extras, with_dataclasses)
        if case is None:
            continue
        for declared in case.classes:
            for path in H.PATHS:
                cell = H.cell(path, declared, case.instance, client)
                what = None
                if cell.outcome in ("LOST", "OTHER"):
                    what = f"reads {cell.outcome} from {H.canon(cell.sent)}"
                elif (form := H.needless(cell, case.instance)) is not None:
                    who = "it" if path == "client" else "every class of the hierarchy"
                    what = f"is REFUSED, though {who} reads the offered {H.canon(form)} EQUAL"
                if what is not None:
                    found.append(
                        Finding(
                            seed,
                            index,
                            f"{path}{stream}: {declared.__name__} {what}",
                            case.reproduction,
                            (cell, case.instance),
                        )
                    )
    return found


def test_every_class_reads_what_each_path_sends_or_a_known_limit_explains_it():
    found = [
        f
        for seed in seeds(FIXED_SEED)
        for f in findings(seed, cases(CASES))
        + findings(seed, cases(EXTRAS_CASES), extras=True)
        + findings(seed, cases(DATACLASS_CASES), with_dataclasses=True)
    ]

    assert_only_known_limits(found, LIMITS, check="H, every class of a hierarchy")


def _alias_dump(value: Any, *_: Any, **__: Any) -> Any:
    _alias_dump.calls += 1  # type: ignore[attr-defined]
    return pydantic_core.to_jsonable_python(value, by_alias=True)


def test_CONTROL_always_the_alias_dump_breaks_the_property(monkeypatch):
    """The same generator and classifier, with every path sending the plain alias dump."""
    _alias_dump.calls = 0  # type: ignore[attr-defined]
    monkeypatch.setattr(validation, "wire_models", _alias_dump)
    monkeypatch.setattr(ServiceClient, "_encode", lambda self, value, declared: _alias_dump(value))
    monkeypatch.setattr(
        kv, "serialize_value", lambda value: H.canon(_alias_dump(value)).encode("utf-8")
    )

    found = findings(FIXED_SEED, CONTROL_CASES)

    assert _alias_dump.calls > 0, "the CONTROL's replacement was never called"  # type: ignore[attr-defined]
    assert_control_finds(
        judge(found, LIMITS).uncovered, at_least=CONTROL_FLOOR, control="always the alias dump"
    )


# --- each limit's pinned example: it matches that limit and no other ---------------------------


class LostBase(BaseModel):
    y: int = Field(0, serialization_alias="Y")


class LostLeaf(LostBase):
    y: int = Field(0, alias="x")


class OtherBase(BaseModel):
    z: int = Field(0, validation_alias="y")
    y: int = Field(0, validation_alias="yy")


class OtherLeaf(OtherBase):
    y: int
    z: int = Field(validation_alias=AliasPath("zz", "k"))


class StoredBase(BaseModel):
    y: str = Field("d", validation_alias=AliasPath("yy", "k"))


class StoredLeaf(StoredBase):
    y: str = Field(alias="Y")


def _pinned(cell: H.Cell, inst: BaseModel) -> Finding:
    return Finding(0, 0, "pinned", "", (cell, inst))


def test_H_W_a_base_reads_the_dumps_the_wire_sends_as_lost():
    inst = LostLeaf(x=6)

    cell = H.cell("wire", LostBase, inst)

    # The literal bytes: a change to what the dumps-only choice sends moves both sides of H-W's
    # comparison, and only this sees it.
    assert (cell.outcome, H.canon(cell.sent)) == ("LOST", '{"x": 6}')
    assert_matches_only(_pinned(cell, inst), "H-W", LIMITS)


def test_H_W_a_base_reads_the_dumps_the_wire_sends_as_other_values():
    inst = OtherLeaf(y=7, zz={"k": 1})

    cell = H.cell("wire", OtherBase, inst)

    assert (cell.outcome, H.canon(cell.sent)) == ("OTHER", '{"y": 7, "z": 1}')
    assert_matches_only(_pinned(cell, inst), "H-W", LIMITS)


def test_H_K1_a_base_reads_what_kv_stores_as_it_reads_the_released_form():
    inst = StoredLeaf(Y="V3")

    cell = H.cell("kv", StoredBase, inst)

    assert (cell.outcome, cell.sent) == ("LOST", b'{"Y":"V3"}')
    assert_matches_only(_pinned(cell, inst), "H-K1", LIMITS)


# --- fixed shapes a send path once misread, each pinned to its outcome on each path ------------


@pytest.mark.parametrize(
    ("value", "declared", "wire", "client"),
    [shape[1:] for shape in false_send_shapes.SHAPES],
    ids=[shape[0] for shape in false_send_shapes.SHAPES],
)
def test_a_fixed_shape_is_read_as_pinned_on_each_path(value, declared, wire, client):
    sent = {path: H.cell(path, declared, value) for path in ("wire", "client")}

    assert {path: cell.outcome for path, cell in sent.items()} == {"wire": wire, "client": client}
    if wire in ("LOST", "OTHER"):
        assert_matches_only(_pinned(sent["wire"], value), "H-W", LIMITS)


# --- a refusal is not needless ------------------------------------------------------------------

NEEDLESS_CONTROL_CASES = 500
#: A third of the needless refusals the NaN CONTROL makes over its cases on main (94 on seed 7; 65
#: and 108 on seeds 20261003 and 424242).
NEEDLESS_CONTROL_FLOOR = 31


def test_CONTROL_a_compare_blind_to_nan_makes_needless_refusals(monkeypatch):
    """The same generator, with the shipped chooser comparing by plain `==`, so a NaN never equals
    the NaN it was sent as, and a value holding one is refused though a dump carries it."""
    calls = []

    def plain(a: Any, b: Any) -> bool:
        calls.append(1)
        try:
            return bool(a == b)
        except Exception:
            return False

    monkeypatch.setattr(validation, "_same", plain)

    found = findings(FIXED_SEED, NEEDLESS_CONTROL_CASES, extras=True)

    assert calls, "the CONTROL's compare was never called"
    needless = [f for f in found if f.detail[0].outcome == "REFUSED"]
    assert_control_finds(
        needless, at_least=NEEDLESS_CONTROL_FLOOR, control="a compare blind to NaN"
    )


class NanModel(BaseModel):
    f: float = 0.0


def test_a_value_holding_nan_is_sent_by_the_client_and_read_back_equal():
    cell = H.cell("client", NanModel, NanModel(f=math.nan))

    assert cell.outcome == "EQUAL"


def test_a_client_refusal_is_needless_when_the_declared_class_reads_an_offered_form(monkeypatch):
    monkeypatch.setattr(validation, "_same", lambda a, b: bool(a == b))
    inst = NanModel(f=math.nan)

    cell = H.cell("client", NanModel, inst)

    assert cell.outcome == "REFUSED"
    assert H.canon(H.needless(cell, inst)) == '{"f": NaN}'


def test_CONTROL_a_client_refusal_no_offered_form_survives_is_not_needless():
    shape = next(s for s in false_send_shapes.SHAPES if s[0] == "two fields read from one key")
    _, value, declared, _, _ = shape

    cell = H.cell("client", declared, value)

    assert cell.outcome == "REFUSED" and cell.offered
    assert H.needless(cell, value) is None


class Plain(BaseModel):
    x: int = 0


class PlainLeaf(Plain):
    pass


def test_a_wire_refusal_is_needless_when_every_class_reads_an_offered_form():
    inst = PlainLeaf(x=5)
    cell = H.Cell("wire", Plain, "REFUSED", offered=({"x": 5},))

    assert H.needless(cell, inst) == {"x": 5}


def test_a_wire_refusal_is_not_needless_when_only_the_leaf_reads_an_offered_form():
    """The leaf reads `{"x": 6}` EQUAL and its base reads it LOST: refusing it is the wire declining
    to send the base other values."""
    inst = LostLeaf(x=6)
    form = inst.model_dump(mode="json", by_alias=True)
    cell = H.Cell("wire", LostBase, "REFUSED", offered=(form,))

    assert (H.cell("wire", LostLeaf, inst).outcome, H.cell("wire", LostBase, inst).outcome) == (
        "EQUAL",
        "LOST",
    )
    assert H.needless(cell, inst) is None


def test_the_plain_dump_the_wire_reads_before_choosing_is_among_the_forms_offered():
    """The wire sends its plain dump without consulting the chooser when the value's own class
    reads it as the argument, so a refusal on that route is judged against it too."""
    cell = H.cell("wire", Plain, PlainLeaf(x=5))

    assert cell.outcome == "EQUAL"
    assert cell.offered == ({"x": 5},)
