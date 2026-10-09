"""Generated models whose fields carry aliases that read other fields' names, and the base classes a
handler declares for them.

A case is a model of one to three fields named from `a`, `b`, `c` and `p`, each with no alias, an
alias, a validation alias, `AliasChoices` of two, or an `AliasPath`, any of which may name another
field. The class may set `populate_by_name`, or read by field name only. A field is an `int`, or a
`dict` for `p`, with or without a default. In about two cases in five the value is an instance of a
subclass that redeclares some of a base's fields with aliases of their own, and the declared class
is the base, as for a handler annotated with the base.

The value has every field set, validated by field name. Each path sends it:

- `client`: `ServiceClient._encode(value, declared)`;
- `wire`: `wire_models(value)`;

and the declared class reads what was sent as the service reads it. A cell is classified as
`hierarchy.classify` classifies it.
"""

from __future__ import annotations

import random
from dataclasses import dataclass
from typing import Any

from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field, create_model

from tests.fixtures.properties import hierarchy as H

NAMES = ["a", "b", "c", "p"]
PATHS = ("client", "wire")


def _alias_kw(rng: random.Random, names: list[str]) -> dict[str, Any]:
    k = rng.random()
    if k < 0.3:
        return {}
    if k < 0.45:
        return {"alias": rng.choice(["A", "B", "a", "b", "q"])}
    if k < 0.6:
        return {"validation_alias": rng.choice(["q", "r", *names])}
    if k < 0.8:
        return {"validation_alias": AliasChoices(*rng.sample(["q", "r", *names], 2))}
    return {"validation_alias": AliasPath(rng.choice(["q", "p", *names]), rng.choice(["k", 0, 1]))}


def _spell(kw: dict[str, Any]) -> str:
    def one(v: Any) -> str:
        if isinstance(v, AliasChoices):
            return f"AliasChoices({', '.join(map(repr, v.choices))})"
        if isinstance(v, AliasPath):
            return f"AliasPath({', '.join(map(repr, v.path))})"
        return repr(v)

    return ", ".join(f"{key}={one(value)}" for key, value in kw.items())


@dataclass(frozen=True)
class Case:
    seed: int
    index: int
    declared: type[BaseModel]
    value: BaseModel
    kind: str
    reproduction: str


def build(seed: int, index: int) -> Case | None:
    """Case `index` of `seed`, or None when pydantic refuses the generated model or its values. Each
    case has its own random stream, so one rebuilds alone."""
    rng = random.Random(seed * 1_000_003 + index)
    names = rng.sample(NAMES, rng.randint(1, 3))
    fields: dict[str, Any] = {}
    lines: list[str] = []
    for n in names:
        kw = _alias_kw(rng, names)
        if n == "p" and rng.random() < 0.5:
            fields[n] = (dict, Field({}, **kw))
            lines.append(f"    {n}: dict = Field({{}}{', ' + _spell(kw) if kw else ''})")
            continue
        default = rng.random() < 0.6
        fields[n] = (int, Field(0, **kw) if default else Field(**kw))
        args = ", ".join(x for x in ("0" if default else "", _spell(kw)) if x)
        lines.append(f"    {n}: int = Field({args})")
    config = rng.choice(
        [
            None,
            ConfigDict(populate_by_name=True),
            ConfigDict(validate_by_alias=False, validate_by_name=True),
        ]
    )
    config_line = (
        f"    model_config = ConfigDict({', '.join(f'{k}={v!r}' for k, v in config.items())})"
        if config
        else ""
    )
    try:
        if rng.random() < 0.6:
            declared = cls = create_model("M", __config__=config, **fields)
            kind = "own"
            source = "\n".join(["class M(BaseModel):", *filter(None, [config_line]), *lines])
        else:
            base_fields: dict[str, Any] = {}
            base_lines: list[str] = []
            for n, (ann, _) in fields.items():
                kw = _alias_kw(rng, names) if rng.random() < 0.3 else {}
                base_fields[n] = (ann, Field({} if ann is dict else 0, **kw))
                base_lines.append(
                    f"    {n}: {ann.__name__} = Field({'{}' if ann is dict else '0'}"
                    f"{', ' + _spell(kw) if kw else ''})"
                )
            declared = create_model("B", __config__=config, **base_fields)
            redeclared = rng.sample(list(fields), rng.randint(1, len(fields)))
            cls = create_model("M", __base__=declared, **{n: fields[n] for n in redeclared})
            kind = "base"
            source = "\n".join(
                ["class B(BaseModel):", *filter(None, [config_line]), *base_lines, "", "",
                 "class M(B):", *(line for line in lines if line.split(":")[0].strip() in redeclared)]
            )  # fmt: skip
    except Exception:
        return None
    values = {
        n: (
            {"k": rng.randint(1, 9)}
            if cls.model_fields[n].annotation is dict
            else rng.randint(1, 99)
        )
        for n in cls.model_fields
    }
    try:
        value = cls.model_validate(values, by_name=True, by_alias=False)
    except Exception:
        return None
    reproduction = (
        "from pydantic import AliasChoices, AliasPath, BaseModel, ConfigDict, Field\n\n"
        f"{source}\n\n# the value is M validated by field name from {values!r}; "
        f"the declared class is {declared.__name__}"
    )
    return Case(seed, index, declared, value, kind, reproduction)


def cell(path: str, case: Case, client: Any = None) -> H.Cell:
    """What the declared class makes of what `path` sends for the case's value."""
    import cliffracer.core.validation as validation

    declared, value = case.declared, case.value
    try:
        if path == "wire":
            sent = validation.wire_models(value)
        else:
            sent = (client or H._client())._encode(value, declared)
    except Exception:
        return H.Cell(path, declared, "REFUSED")
    try:
        read = validation.read_python_then_json(
            sent, declared.model_validate, declared.model_validate_json
        )
    except Exception:
        return H.Cell(path, declared, "REFUSED", sent)
    return H.Cell(path, declared, H.classify(declared, value, read), sent)
