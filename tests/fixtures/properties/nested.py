"""A generated hierarchy's instance held in a field of an outer model that declares one of its
classes: `Outer(item: C)` for each model class C of the leaf's hierarchy.

The cases are `hierarchy`'s. Each path sends the outer model, and the declared class is read from
the outer one the service reads:

- `wire`: `wire_models(outer)`;
- `client`: `ServiceClient._encode(outer, Outer)`.

A cell is classified as `hierarchy.classify` classifies it, on the item.
"""

from __future__ import annotations

from typing import Any

from pydantic import BaseModel, create_model

from tests.fixtures.properties import hierarchy as H

PATHS = ("wire", "client")


def outer(declared: type[BaseModel], inst: BaseModel) -> tuple[type[BaseModel], BaseModel] | None:
    """`Outer(item: declared)` and an instance of it holding `inst`, or None when the outer model
    cannot be built for this class."""
    try:
        model = create_model("Outer", item=(declared, ...))
        return model, model.model_construct(item=inst)
    except Exception:
        return None


def cell(path: str, declared: type[BaseModel], inst: BaseModel, client: Any = None) -> H.Cell:
    """What `declared` makes of the item, inside what `path` sends for an `Outer` holding `inst`."""
    import cliffracer.core.validation as validation

    built = outer(declared, inst)
    if built is None:
        return H.Cell(path, declared, "REFUSED")
    model, value = built
    try:
        if path == "wire":
            sent = validation.wire_models(value)
        else:
            sent = (client or H._client())._encode(value, model)
    except Exception:
        return H.Cell(path, declared, "REFUSED")
    try:
        read = validation.read_python_then_json(
            sent, model.model_validate, model.model_validate_json
        )
    except Exception:
        return H.Cell(path, declared, "REFUSED", sent)
    return H.Cell(path, declared, H.classify(declared, inst, read.item), sent)
