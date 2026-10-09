"""Generated model hierarchies with aliases, and how each class in one reads what each path sends.

A case is a random hierarchy of one to four model classes (a chain, a diamond, or a chain with a
non-model mixin), generated as Python source so a failure prints a reproduction. Each field of each
class gets no alias, an alias, a serialization alias, a validation alias (a string, `AliasChoices`
or `AliasPath`, whose members may name other fields), or a serialization and a validation alias.
Each class may set `populate_by_name`, by-name-only, by-alias-only, `serialize_by_alias`, `strict`
or `extra="forbid"`. A field is required or defaulted, may carry a normalising validator, and a
subclass may redeclare a field with another alias. The instance is the leaf class with every field
set, some left at their default.

A handler may declare any model class of the leaf's hierarchy, so every one of them reads what
each path sends:

- `wire`: `wire_models(instance)`, what `call_rpc`, `call_async` and `RpcProxy` send;
- `client`: `ServiceClient._encode(instance, declared)`, what a generated client sends;
- `kv`: `serialize_value(instance)`, a KV write, read with `deserialize_value(data, as_type=declared)`.

The wire and client values are read as the service reads them (`read_python_then_json`). A cell is
classified on the declared class's own fields:

- EQUAL: every field reads back as the declared class itself makes of the instance's value;
- REFUSED: the client refuses before sending, or the reader raises;
- LOST: a field the instance set reads back as the declared class's default;
- OTHER: any other difference.

The subjects are looked up on their modules at each call, so a CONTROL can replace one with
`monkeypatch`.
"""

from __future__ import annotations

import contextlib
import dataclasses
import json
import math
import random
from collections.abc import Iterator
from dataclasses import dataclass, field
from typing import Any

import pydantic_core
from pydantic import BaseModel

import cliffracer.core.validation as _validation
from tests.fixtures.properties import Finding, Limit

FIELDS = ["x", "y", "z", "p"]

PRELUDE = (
    "from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, field_validator\n"
)

CONFIGS = [
    "model_config = ConfigDict(populate_by_name=True)",
    "model_config = ConfigDict(validate_by_name=True, validate_by_alias=False)",
    "model_config = ConfigDict(validate_by_name=False, validate_by_alias=True)",
    "model_config = ConfigDict(serialize_by_alias=True)",
    "model_config = ConfigDict(serialize_by_alias=True, populate_by_name=True)",
    "model_config = ConfigDict(strict=True)",
    "model_config = ConfigDict(extra='forbid')",
]

DEFAULTS = {
    "dict": "{}",
    "str": '"d"',
    "int": "0",
    "float": "0.0",
    "Point": "Point()",
    "PPoint": "PPoint()",
}

#: Defined before a `dataclasses` case's classes: a standard and a Pydantic dataclass, each with a
#: float a case fills with 1.5, NaN, inf or -inf.
DATACLASS_PRELUDE = (
    "import dataclasses\n"
    "from pydantic.dataclasses import dataclass as pydantic_dataclass\n\n\n"
    "@dataclasses.dataclass\nclass Point:\n    g: float = 0.0\n    h: int = 0\n\n\n"
    "@pydantic_dataclass\nclass PPoint:\n    g: float = 0.0\n    h: int = 0\n"
)

#: What a float field holds in an `extras` case: NaN, which equals nothing, and both infinities.
FLOATS = [1.5, math.nan, math.inf, -math.inf]

PATHS = ("wire", "client", "kv")


def _alias_spec(rng: random.Random, name: str, others: list[str]) -> str:
    """The `Field(...)` keyword text for one field's alias, or '' for none."""
    fresh = name * 2
    other = rng.choice(others)
    kind = rng.choice(
        ["none", "none", "alias", "alias_other", "ser", "val", "val_other", "choices",
         "choices_other_first", "path", "path_other_head", "ser_val", "ser_val_other"]
    )  # fmt: skip
    return {
        "none": "",
        "alias": f'alias="{name.upper()}"',
        "alias_other": f'alias="{other}"',
        "ser": f'serialization_alias="{name.upper()}"',
        "val": f'validation_alias="{fresh}"',
        "val_other": f'validation_alias="{other}"',
        "choices": f'validation_alias=AliasChoices("{fresh}", "{name}")',
        "choices_other_first": f'validation_alias=AliasChoices("{other}", "{name}")',
        "path": f'validation_alias=AliasPath("{fresh}", "k")',
        "path_other_head": f'validation_alias=AliasPath("{other}", "k")',
        "ser_val": f'serialization_alias="{name.upper()}", validation_alias="{fresh}"',
        "ser_val_other": (
            f'serialization_alias="{name.upper()}", '
            f'validation_alias=AliasChoices("{fresh}", "{other}")'
        ),
    }[kind]


def _class_source(
    rng: random.Random, name: str, bases: list[str], inherited: list[str], types: dict[str, str]
) -> tuple[str, list[str]]:
    lines = [f"class {name}({', '.join(bases)}):"]
    if rng.random() < 0.45:
        lines.append("    " + rng.choice(CONFIGS))
    declared: list[str] = []
    # A subclass may redeclare inherited fields, and may add new ones.
    pool = [f for f in FIELDS if f not in inherited]
    chosen = (rng.sample(inherited, k=rng.randint(0, len(inherited))) if inherited else []) + (
        rng.sample(pool, k=rng.randint(0 if inherited else 1, min(2, len(pool)))) if pool else []
    )
    for f in chosen:
        # One type per field name across the hierarchy: a redeclaration changes the alias only.
        ann = types[f]
        spec = _alias_spec(rng, f, [o for o in FIELDS if o != f])
        required = rng.random() < 0.25
        args = ([] if required else [DEFAULTS[ann]]) + ([spec] if spec else [])
        lines.append(f"    {f}: {ann} = Field({', '.join(args)})" if args else f"    {f}: {ann}")
        if ann == "str" and rng.random() < 0.3:
            lines.append(f"    @field_validator('{f}')")
            lines.append("    @classmethod")
            lines.append(f"    def _norm_{f}(cls, v): return v.lower()")
        declared.append(f)
    if all(line.startswith(("class ", "    model_config")) for line in lines):
        lines.append("    pass")
    return "\n".join(lines), declared


def generate(rng: random.Random, extras: bool = False, with_dataclasses: bool = False) -> str:
    """Source for one hierarchy, after `PRELUDE`; the leaf class is named `Leaf`. With `extras`, a
    field other than `p` is a `float` one time in four. With `with_dataclasses` (after
    `DATACLASS_PRELUDE`), a field other than `p` is a `float`, a standard dataclass `Point` or a
    Pydantic dataclass `PPoint`, each one time in four."""
    types = {f: ("dict" if f == "p" else ("str" if rng.random() < 0.3 else "int")) for f in FIELDS}
    if extras:
        for f in FIELDS:
            if f != "p" and rng.random() < 0.25:
                types[f] = "float"
    if with_dataclasses:
        for f in FIELDS:
            if f != "p":
                types[f] = rng.choice(["float", "Point", "PPoint", types[f]])
    parts = ["class Mixin:\n    def helper(self): return 1"]
    shape = rng.choice(["chain", "chain", "chain", "diamond", "mixin"])
    depth = rng.randint(1, 4)
    if shape == "diamond":
        src, f1 = _class_source(rng, "Root", ["BaseModel"], [], types)
        parts.append(src)
        src, f2 = _class_source(rng, "Left", ["Root"], f1, types)
        parts.append(src)
        left = sorted(set(f1) | set(f2))
        src, f3 = _class_source(rng, "Right", ["Root"], f1, types)
        parts.append(src)
        right = sorted(set(f1) | set(f3))
        src, _ = _class_source(
            rng, "Leaf", ["Left", "Right"], sorted(set(left) | set(right)), types
        )
        parts.append(src)
        return "\n\n\n".join(parts)
    prev = "BaseModel"
    inherited: list[str] = []
    for level in range(depth - 1):
        name = f"L{level}"
        bases = [prev] + (["Mixin"] if shape == "mixin" and level == 0 else [])
        src, declared = _class_source(rng, name, bases, inherited, types)
        parts.append(src)
        inherited = sorted(set(inherited) | set(declared))
        prev = name
    bases = [prev] + (["Mixin"] if shape == "mixin" and depth == 1 else [])
    src, _ = _class_source(rng, "Leaf", bases, inherited, types)
    parts.append(src)
    return "\n\n\n".join(parts)


def instance(rng: random.Random, leaf: type[BaseModel]) -> BaseModel:
    """The leaf with every field set, some at their default, built as a caller builds it: the values
    are validated by field name through the class, so a normalising validator's result is what the
    instance holds. Where the class cannot take them by name, the constructed instance stands."""
    inst = leaf.model_construct()
    for name, info in leaf.model_fields.items():
        if rng.random() < 0.2 and not info.is_required():
            setattr(inst, name, info.get_default(call_default_factory=True))
            continue
        if info.annotation is dict:
            setattr(inst, name, {"k": rng.randint(1, 9)})
        elif info.annotation is str:
            setattr(inst, name, rng.choice(["v1", "v2", "V3"]))
        elif info.annotation is float:
            setattr(inst, name, rng.choice(FLOATS))
        elif dataclasses.is_dataclass(info.annotation):
            setattr(inst, name, info.annotation(g=rng.choice(FLOATS), h=rng.randint(1, 9)))
        else:
            setattr(inst, name, rng.randint(1, 9))
    try:
        return leaf.model_validate(
            {name: getattr(inst, name) for name in leaf.model_fields if hasattr(inst, name)},
            by_name=True,
            by_alias=False,
            strict=False,
        )
    except Exception:
        return inst


def expected(cls: type[BaseModel], inst: BaseModel) -> dict[str, Any]:
    """What `cls` holds for the instance's values: each field by name, through `cls`'s validators,
    so a normalising validator's result is not a difference. Where `cls` cannot take them by name,
    the raw values stand."""
    raw = {name: getattr(inst, name, None) for name in cls.model_fields if hasattr(inst, name)}
    try:
        built = cls.model_validate(raw, by_name=True, by_alias=False, strict=False)
        return {name: getattr(built, name) for name in raw}
    except Exception:
        return raw


def same(a: Any, b: Any) -> bool:
    """`a == b`, with a NaN equal to a NaN, inside dataclasses of one class too."""
    if isinstance(a, float) and isinstance(b, float) and a != a and b != b:
        return True
    if (
        dataclasses.is_dataclass(a)
        and not isinstance(a, type)
        and type(a) is type(b)
        and all(same(getattr(a, f.name), getattr(b, f.name)) for f in dataclasses.fields(a))
    ):
        return True
    return bool(a == b)


def classify(cls: type[BaseModel], inst: BaseModel, read: Any) -> str:
    """EQUAL, LOST or OTHER for what `cls` read, on the fields `cls` declares (NaN equal to NaN)."""
    want_all = expected(cls, inst)
    for name, info in cls.model_fields.items():
        want = want_all.get(name, getattr(inst, name, None))
        got = getattr(read, name, None)
        if same(got, want):
            continue
        default = None if info.is_required() else info.get_default(call_default_factory=True)
        if not info.is_required() and got == default and want != default:
            return "LOST"
        return "OTHER"
    return "EQUAL"


def canon(value: Any) -> str:
    """One spelling of a sent value, to compare two of them."""
    if isinstance(value, bytes):
        value = json.loads(value)
    return json.dumps(value, sort_keys=True, default=str)


@dataclass(frozen=True)
class Case:
    seed: int
    index: int
    source: str
    leaf: type[BaseModel]
    instance: BaseModel

    @property
    def classes(self) -> list[type[BaseModel]]:
        """Every model class a handler could declare for the instance: the leaf's hierarchy."""
        return [
            c
            for c in self.leaf.__mro__
            if isinstance(c, type) and issubclass(c, BaseModel) and c is not BaseModel
        ]

    @property
    def reproduction(self) -> str:
        values = {name: getattr(self.instance, name, None) for name in self.leaf.model_fields}
        return f"{PRELUDE}\n{self.source}\n\n# the instance holds {values!r}"


def build(
    seed: int, index: int, extras: bool = False, with_dataclasses: bool = False
) -> Case | None:
    """Case `index` of `seed`, or None when its generated classes do not define (pydantic refuses
    some alias combinations). Each case has its own random stream, so one rebuilds alone; the
    `extras` cases and the `with_dataclasses` cases are streams of their own."""
    if with_dataclasses:
        rng = random.Random(f"{seed}:{index}:dataclasses")
    else:
        rng = random.Random(f"{seed}:{index}:extras" if extras else seed * 1_000_003 + index)
    source = (DATACLASS_PRELUDE + "\n\n" if with_dataclasses else "") + generate(
        rng, extras, with_dataclasses
    )
    namespace: dict[str, Any] = {}
    try:
        exec(compile(PRELUDE + source, f"<case {seed}:{index}>", "exec"), namespace)
        # A generated class names the case's own dataclasses, which no module defines: resolve its
        # annotations against the namespace the case was run in.
        for value in namespace.values() if with_dataclasses else ():
            if isinstance(value, type) and issubclass(value, BaseModel) and value is not BaseModel:
                value.model_rebuild(force=True, _types_namespace=namespace)
    except Exception:
        return None
    leaf = namespace["Leaf"]
    return Case(seed, index, source, leaf, instance(rng, leaf))


@dataclass(frozen=True)
class Cell:
    """How the declared class read what one path sent: the classification, and what was sent."""

    path: str
    declared: type[BaseModel]
    outcome: str
    sent: Any = None
    #: Every form the shipped chooser was offered while the path sent (see `offered_forms`).
    offered: tuple[Any, ...] = field(default=(), compare=False)


def _client() -> Any:
    from cliffracer.client import ServiceClient

    class NoConnection:
        pass

    return ServiceClient(NoConnection(), service="s", verify=False)


def cell(path: str, declared: type[BaseModel], inst: BaseModel, client: Any = None) -> Cell:
    """What `declared` makes of what `path` sends for `inst`."""
    import cliffracer.core.validation as validation

    if path == "kv":
        import cliffracer_kv.serialization as kv

        try:
            data = kv.serialize_value(inst)
        except Exception:
            return Cell(path, declared, "REFUSED")
        try:
            read = kv.deserialize_value(data, as_type=declared)
        except Exception:
            return Cell(path, declared, "REFUSED", data)
        return Cell(path, declared, classify(declared, inst, read), data)
    with offered_forms(inst) as offered:
        try:
            if path == "wire":
                sent = validation.wire_models(inst)
            else:
                sent = (client or _client())._encode(inst, declared)
        except Exception:
            return Cell(path, declared, "REFUSED", offered=tuple(offered))
    try:
        read = validation.read_python_then_json(
            sent, declared.model_validate, declared.model_validate_json
        )
    except Exception:
        return Cell(path, declared, "REFUSED", sent, tuple(offered))
    return Cell(path, declared, classify(declared, inst, read), sent, tuple(offered))


@contextlib.contextmanager
def offered_forms(value: Any) -> Iterator[list[Any]]:
    """Record every form of `value` the shipped send path considers while the block runs.

    Those are the forms `choose_wire_form` makes, and the form the wire reads before reaching it
    (its plain dump, which `_reads_as_the_argument` checks first, or which a plain model is sent as
    without that check, when `_is_plain_instance` takes it). The shipped functions still
    decide: each is called through unchanged, and only what it is given is noted. Forms of the
    models nested inside `value`, which the same functions are given level by level, are not forms
    of `value` and are not noted; a form that raises `FormUnavailable` was not offered.
    """
    import cliffracer.client as client_module

    chooser = _validation.choose_wire_form
    reads = _validation._reads_as_the_argument
    plain = _validation._is_plain_instance
    made: list[Any] = []

    def choosing(subject: Any, forms: Any, *args: Any, **kwargs: Any) -> Any:
        if subject is not value:
            return chooser(subject, forms, *args, **kwargs)

        def noted(make: Any) -> Any:
            def make_and_note() -> Any:
                form = make()
                made.append(form)
                return form

            return make_and_note

        return chooser(subject, tuple(noted(make) for make in forms), *args, **kwargs)

    def taken_as_plain(subject: Any) -> bool:
        took = plain(subject)
        if took and subject is value:
            made.append(pydantic_core.to_jsonable_python(subject))
        return took

    def reading(cls: Any, wire: Any, subject: Any) -> bool:
        if subject is value and not any(wire is form for form in made):
            made.append(wire)
        return reads(cls, wire, subject)

    _validation.choose_wire_form = choosing  # type: ignore[assignment]
    client_module.choose_wire_form = choosing  # type: ignore[assignment]
    _validation._reads_as_the_argument = reading  # type: ignore[assignment]
    _validation._is_plain_instance = taken_as_plain  # type: ignore[assignment]
    try:
        yield made
    finally:
        _validation.choose_wire_form = chooser
        client_module.choose_wire_form = chooser
        _validation._reads_as_the_argument = reads
        _validation._is_plain_instance = plain


# The limits judge against the shipped function, even while a CONTROL replaces it.
_wire_models = _validation.wire_models


def dumps_only(value: Any) -> str | None:
    """What the wire sends for `value` choosing among the dumps alone, or None when it refuses."""
    try:
        return canon(_wire_models(value, extra_forms="none"))
    except Exception:
        return None


def _wire_sent_the_dumps(finding: Finding) -> bool:
    cell, value = finding.detail
    return (
        cell.path == "wire"
        and cell.outcome in ("LOST", "OTHER")
        and canon(cell.sent) == dumps_only(value)
    )


#: A finding's `detail` is `(cell, value)`: the cell, and the value the path was given to send.
H_W = Limit(
    "H-W",
    "the wire sent the dumps it chooses among alone, what it sent before the validation-alias form "
    "existed: a handler declaring a base gets what it got before",
    _wire_sent_the_dumps,
)


def _reads_equal(cls: type[BaseModel], form: Any, inst: BaseModel) -> bool:
    try:
        read = _validation.read_python_then_json(form, cls.model_validate, cls.model_validate_json)
    except Exception:
        return False
    return classify(cls, inst, read) == "EQUAL"


def needless(cell: Cell, inst: BaseModel) -> Any:
    """For a REFUSED cell, an offered form the path could have sent correctly, else None.

    The client knows the declared class: a form that class reads back EQUAL would have been
    delivered correctly. The wire does not: only a form every model class of the instance's
    hierarchy reads back EQUAL would have been (a refusal where the leaf reads a form EQUAL and a
    base misreads it is the wire refusing to send a base other values, which is right). KV has its
    own rule for refusals and is not judged here.
    """
    if cell.outcome != "REFUSED" or cell.path not in ("client", "wire"):
        return None
    readers = [cell.declared] if cell.path == "client" else _validation._model_classes(inst)
    for form in cell.offered:
        if all(_reads_equal(cls, form, inst) for cls in readers):
            return form
    return None
