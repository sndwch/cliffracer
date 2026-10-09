"""Property: a template accepts a settings value exactly when the document it stores reads it back.

Settings models are generated from a seed (`tests.fixtures.properties.templates`): stdlib
dataclasses, models and pydantic dataclasses under the alias configs, held directly or in lists,
tuples, dicts, sets and root models, with explicit validation and serialization aliases,
`init=False` fields and a serializer that changes a value. Each instance is normalised by a
registered template, and so are two mappings a caller could give for it, each judged by the model it
validates to: its Python dump by alias, which a serializer has written, and its fields' values as
they are held, each under a key the model reads. The oracle (`reads_back`) says whether the document
pydantic writes for a value reads back as it, every scalar changed on its own read back changed. In
both directions:
- a value `normalize` accepts must read back, or it is lost once stored;
- a value that reads back must be accepted, or a valid setting is refused;
- a value that does not must be refused with `TemplateError`, as documented.
One limit is known, and the template's document states it (`docs/service-templates.md`): a field
with one allowed value, written under a key the schema does not read, is refused, since no different
valid value can be built to show the key is read. Its one value cannot be lost, so the oracle calls
it read back. Any other outcome fails.

The CONTROL normalises the same values with the per-field check skipped, and must find values
accepted that do not read back.
"""

import random
import re
from typing import Literal

import pytest
from pydantic import AliasChoices, AliasPath, BaseModel, Field, ValidationError

import cliffracer.runners.templates as templates_module
from cliffracer.runners import TemplateCatalog
from cliffracer.runners.contracts import TemplateError
from tests.fixtures.properties import (
    Finding,
    Limit,
    assert_control_finds,
    assert_matches_only,
    assert_only_known_limits,
    cases,
    seeds,
)
from tests.fixtures.properties.templates import generate, reads_back
from tests.fixtures.shipment_templates import shipment_template

pytestmark = pytest.mark.unit

SEED = 7
CASES = 300
CONTROL_CASES = 150
#: A third of what the CONTROL found when measured on seed 7: 110 values accepted that do not read
#: back, over the instance and the two mappings of 150 cases.
CONTROL_FLOOR = 36


def _described(model) -> str:
    return "\n".join(f"{name}: {field.annotation!r}" for name, field in model.model_fields.items())


def findings(count: int = CASES) -> list[Finding]:
    found: list[Finding] = []
    for seed in seeds(SEED):
        rng = random.Random(seed)
        for index in range(cases(count)):
            model, instance = generate(rng, index)
            for finding in (
                _finding_of(seed, index, model, instance, instance),
                _mapping_finding_of(seed, index, model, _dumped(instance), "dumped mapping: "),
                _mapping_finding_of(seed, index, model, _held(model, instance), "held mapping: "),
            ):
                if finding is not None:
                    found.append(finding)
    return found


def _dumped(instance) -> dict | None:
    try:
        return instance.model_dump(mode="python", by_alias=True, round_trip=True)
    except (TypeError, ValueError):
        return None


def _read_key(field, name: str) -> str:
    """A key the model reads `name` from: its validation alias (a string, the first string choice,
    or a one-key path), its alias, or its name."""
    alias = field.validation_alias
    if isinstance(alias, AliasChoices):
        alias = next((choice for choice in alias.choices if isinstance(choice, str)), None)
    if isinstance(alias, AliasPath) and len(alias.path) == 1 and isinstance(alias.path[0], str):
        alias = alias.path[0]
    if isinstance(alias, str):
        return alias
    return field.alias or name


def _held(model, instance) -> dict:
    held = {
        _read_key(field, name): getattr(instance, name)
        for name, field in model.model_fields.items()
    }
    return held | dict(instance.model_extra or {})


def _mapping_finding_of(seed: int, index: int, model, mapping, route: str) -> Finding | None:
    """What breaks the invariant when `mapping` is normalised. A mapping that does not validate may
    be refused in any way; one that does is judged by the model it validates to, which is what the
    template stores."""
    if mapping is None:
        return None
    try:
        judged = model.model_validate(mapping)
    except (ValidationError, TypeError, ValueError):
        return None
    return _finding_of(seed, index, model, mapping, judged, route=route)


def _finding_of(seed: int, index: int, model, given, judged, route: str = "") -> Finding | None:
    """What breaks the invariant when `given` is normalised, judged by the model `judged`."""
    registered = TemplateCatalog().register(shipment_template(settings_model=model))
    try:
        registered.normalize(given)
        outcome = "accepted"
    except TemplateError as refused:
        outcome = f"refused ({refused})"
    except Exception as exc:
        what = f"{route}normalize raised {type(exc).__name__}: {exc}"[:300]
        return Finding(seed, index, what, _described(model), {"kind": "raised", "model": model})
    good, why = reads_back(model, judged)
    if outcome == "accepted" and not good:
        detail = {"kind": "accepted", "model": model}
        return Finding(seed, index, f"{route}accepted, but {why}", _described(model), detail)
    if outcome != "accepted" and good:
        what = f"{route}{outcome}, but the document reads back"
        return Finding(seed, index, what, _described(model), {"kind": "refused", "model": model})
    return None


def _models_in(model) -> list:
    """`model` and every model class its fields hold, at any depth."""
    found, stack = [], [model]
    while stack:
        current = stack.pop()
        if isinstance(current, type) and issubclass(current, BaseModel) and current not in found:
            found.append(current)
            stack += [field.annotation for field in current.model_fields.values()]
        for argument in getattr(current, "__args__", ()):
            stack.append(argument)
    return found


#: The keys each one-value field is read from (its `AliasChoices`, its one-key `AliasPath`).
_READ_FROM = {"tag": {"tag_c", "tag"}, "mark": {"mark"}}


def _one_value_whose_key_is_not_read(finding: Finding) -> bool:
    """A field with one allowed value is refused, and a model holding it writes it under a key it is
    not read from: a serialization alias, or an alias from the model's alias generator."""
    named = re.search(r"settings field '(tag|mark)' must round-trip", finding.what)
    if finding.detail["kind"] != "refused" or not named:
        return False
    name = named.group(1)
    return any(
        (field := holder.model_fields.get(name)) is not None
        and (field.serialization_alias or field.alias or name) not in _READ_FROM[name]
        for holder in _models_in(finding.detail["model"])
    )


LIMITS = [
    Limit(
        "T-L1 a field with one allowed value, written under a key it is not read from",
        "documented: the field is refused when no different valid value can be built for it",
        _one_value_whose_key_is_not_read,
    ),
]


def test_a_template_accepts_exactly_the_settings_that_read_back():
    assert_only_known_limits(findings(), LIMITS, check="T (template settings)")


class OneValue(BaseModel):
    tag: Literal["a"] = Field(
        "a", validation_alias=AliasChoices("tag_c", "tag"), serialization_alias="one_out"
    )


def test_the_limit_covers_its_pinned_example_and_nothing_else_does():
    value = OneValue()
    finding = _finding_of(7, 0, OneValue, value, value)

    assert finding is not None
    assert_matches_only(finding, LIMITS[0].name, LIMITS)


def test_CONTROL_with_the_field_check_skipped_values_that_do_not_read_back_are_accepted(
    monkeypatch,
):
    calls: list[object] = []
    monkeypatch.setattr(
        templates_module, "_check_settings_inputs", lambda value, *_, **__: calls.append(value)
    )

    accepted = [f for f in findings(CONTROL_CASES) if f.detail["kind"] == "accepted"]

    assert len(calls) > 0, "the skipped check is not the one normalize calls"
    assert_control_finds(accepted, at_least=CONTROL_FLOOR, control="field check skipped")
