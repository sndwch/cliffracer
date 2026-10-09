"""Models an event publishes: nested, with aliases, validation aliases and alias-related configs.

`generate(rng, index, extras)` builds one model class (by `create_model`) and an instance of it, and
describes both in text for a failure to print, since the classes have no source. A class may be
frozen, so an instance whose models are all frozen can be put in a set. With `extras`, a field that
is not a nested model is, one time in five each, a float holding 1.5, NaN or infinity, or an int
whose serializer writes a changed value by field name only.
"""

from __future__ import annotations

import random
from typing import Annotated, Any

from pydantic import AliasChoices, BaseModel, ConfigDict, Field, PlainSerializer, create_model

CONFIGS: list[dict[str, Any]] = [
    {},
    {"populate_by_name": True},
    {"validate_by_alias": False, "validate_by_name": True},
    {"validate_by_alias": True, "validate_by_name": False},
    {"serialize_by_alias": True},
    {"frozen": True},
]


def _by_name_only(value: int, info: Any) -> int:
    """A serializer that writes a changed value by field name and the value itself by alias."""
    return value if info.by_alias else value + 100


def _model(rng: random.Random, extras: bool, depth: int, index: int, lines: list[str]):
    count = rng.randint(1, 3)
    fields: dict[str, Any] = {}
    values: dict[str, Any] = {}
    names = [f"f{k}" for k in range(count)]
    described: list[str] = []
    for k, name in enumerate(names):
        r = rng.random()
        options: dict[str, Any] = {}
        if r < 0.45:
            options["alias"] = rng.choice(
                [name.upper(), name + "_a", names[(k + 1) % count] if count > 1 else name + "x"]
            )
        elif r < 0.6:
            options["validation_alias"] = AliasChoices(name + "_v", name)
        elif r < 0.7:
            options["serialization_alias"] = name + "_s"
        if depth < 2 and rng.random() < 0.25:
            sub, sub_value = _model(rng, extras, depth + 1, index, lines)
            fields[name] = (sub, Field(**options))
            values[name] = sub_value
            kind = sub.__name__
        elif extras and rng.random() < 0.2:
            fields[name] = (float, Field(**options))
            values[name] = rng.choice([1.5, float("nan"), float("inf")])
            kind = "float"
        elif extras and rng.random() < 0.2:
            fields[name] = (Annotated[int, PlainSerializer(_by_name_only)], Field(**options))
            values[name] = rng.randint(0, 99)
            kind = "int, written +100 by field name only"
        else:
            fields[name] = (int, Field(**options))
            values[name] = rng.randint(0, 99)
            kind = "int"
        described.append(f"  {name}: {kind} {options or ''} = {values[name]!r}")
    config = rng.choice(CONFIGS)
    model = create_model(f"M{index}_{depth}", __config__=ConfigDict(**config), **fields)
    lines.append(f"class {model.__name__} config={config}:\n" + "\n".join(described))
    try:
        plain = {
            name: value.model_dump(by_alias=False) if isinstance(value, BaseModel) else value
            for name, value in values.items()
        }
        instance = model.model_validate(plain, by_alias=False, by_name=True)
    except Exception:
        instance = None
    return model, instance


def generate(rng: random.Random, index: int, extras: bool):
    """One model class, an instance of it (None when the class refuses the generated values), and a
    description of both."""
    lines: list[str] = []
    model, instance = _model(rng, extras, 0, index, lines)
    return model, instance, "\n".join(lines)


def field_values(value: Any) -> Any:
    """A model's field values by name, nested models included, a NaN as the string "NaN"."""
    if isinstance(value, BaseModel):
        return {name: field_values(getattr(value, name)) for name in type(value).model_fields}
    if isinstance(value, float) and value != value:
        return "NaN"
    return value
