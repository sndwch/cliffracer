"""Property: a model an event carries arrives with the field values it was published with, or is
refused; it never arrives holding other values.

Model classes and instances are generated from a seed (`tests.fixtures.properties.published`):
nested models, aliases that are other fields' names, validation aliases and the alias-related
configs, and in the extras run (half the cases) fields that are floats holding 1.5, NaN or infinity
or ints with a serializer that writes a changed value by field name only. Each instance is
published with `publish_event`, in JSON or MessagePack, on its own or in a list, a tuple, a set,
a frozenset (a set only when its models are frozen) or an `OrderedDict`, onto a mocked connection,
and the bytes it put on the wire are read as a listener reads them (`deserialize_payload`, then
`validate_decoded` with the model class). The outcome must be the same field values, a refusal by
the listener, or `publish_event` refusing it before sending with an `RpcValidationError`; and a
refusal, at either end, only where no form of the instance arrives whole: by alias, by field name,
or one model at a time. No limit is known: any other outcome fails.

The CONTROL publishes each model by field name always, and must find models arriving changed.
"""

import contextlib
import random
from collections import OrderedDict
from collections.abc import Iterator
from unittest.mock import AsyncMock

import pydantic_core
import pytest
from loguru import logger
from pydantic import BaseModel, Field, SerializationInfo, field_serializer

import cliffracer.core.service as service_module
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import (
    deserialize_payload,
    nested_form,
    serialize_payload,
    validate_decoded,
)
from tests.fixtures.properties import (
    Finding,
    assert_control_finds,
    assert_only_known_limits,
    cases,
    seeds,
)
from tests.fixtures.properties.published import field_values, generate

pytestmark = pytest.mark.unit

SEED = 7
CASES = 750  # each of the plain and the extras runs
CONTROL_CASES = 300
#: A third of what the CONTROL found when measured on seed 7: 73 changed arrivals in 600 cases.
CONTROL_FLOOR = 24


@contextlib.contextmanager
def _quiet() -> Iterator[None]:
    """`publish_event` logs every event; a thousand of them only bury a failure."""
    logger.disable("cliffracer")
    try:
        yield
    finally:
        logger.enable("cliffracer")


def _service() -> CliffracerService:
    service = CliffracerService(
        ServiceConfig(name="pub", health_port=0, serialization_format="json")
    )
    service.nc = AsyncMock()
    service.container.nc = service.nc
    return service


def _a_form_that_arrives_whole(model, instance, encoding: str) -> str | None:
    """Which form of the instance, sent in `encoding` and read as a listener reads it, arrives with
    every field value the instance holds, if one does: a refusal is then not needed. The forms are
    its dumps by alias and by field name, and `nested_form`, which writes each model in the form its
    own class reads; whichever is tried, reading it back decides."""
    forms = {
        "by alias": lambda: instance.model_dump(mode="json", by_alias=True),
        "by field name": lambda: instance.model_dump(mode="json", by_alias=False),
        "one model at a time": lambda: nested_form(instance, alias_first=True),
    }
    for label, make in forms.items():
        try:
            raw, _ = serialize_payload(make(), encoding)
            back = validate_decoded(model, deserialize_payload(raw, None, fallback_format=encoding))
        except Exception:
            continue
        if field_values(back) == field_values(instance):
            return label
    return None


def _hashable(instance) -> bool:
    try:
        hash(instance)
    except TypeError:
        return False
    return True


async def findings(count: int = CASES, attempts: list[int] | None = None) -> list[Finding]:
    """The findings of a run; `attempts`, when given, gets one entry for each publish tried."""
    found: list[Finding] = []
    service = _service()
    with _quiet():
        found += await _published(service, count, [] if attempts is None else attempts)
    return found


async def _published(service: CliffracerService, count: int, attempts: list[int]) -> list[Finding]:
    found: list[Finding] = []
    for seed in seeds(SEED):
        for extras in (False, True):
            rng = random.Random(f"{seed}-{'extras' if extras else 'plain'}")
            for index in range(cases(count)):
                model, instance, described = generate(rng, index, extras)
                if instance is None:
                    continue
                encoding = rng.choice(["json", "msgpack"])
                container = rng.choice([None, None, list, tuple, set, frozenset, OrderedDict])
                if container in (set, frozenset) and not _hashable(instance):
                    container = list
                service.config.serialization_format = encoding
                attempts.append(index)
                try:
                    if container is None:
                        await service.publish_event("t.s", item=instance)
                    elif container is OrderedDict:
                        await service.publish_event("t.s", items=OrderedDict(k=instance))
                    else:
                        await service.publish_event("t.s", items=container([instance]))
                except RpcValidationError as refused:
                    carried = _a_form_that_arrives_whole(model, instance, encoding)
                    if carried is not None:
                        found.append(
                            Finding(
                                seed,
                                index,
                                f"refused before publishing in {encoding} ({refused}), though its "
                                f"form {carried} arrives with every field value",
                                described,
                                "refused",
                            )
                        )
                    continue
                except Exception as exc:
                    found.append(
                        Finding(seed, index, f"publish_event raised {exc!r}", described, "raised")
                    )
                    continue
                raw = service.nc.publish.await_args.args[1]
                data = deserialize_payload(raw, content_type=None, fallback_format=encoding)
                items = data["data"].get("items")
                sent = (
                    data["data"]["item"]
                    if container is None
                    else items[0 if container is not OrderedDict else "k"]
                )
                try:
                    back = validate_decoded(model, sent)
                except Exception as refused:
                    carried = _a_form_that_arrives_whole(model, instance, encoding)
                    if carried is not None:
                        found.append(
                            Finding(
                                seed,
                                index,
                                f"published in {encoding} as {sent!r}, refused by the listener "
                                f"({type(refused).__name__}), though its form {carried} arrives "
                                "with every field value",
                                described,
                                "refused",
                            )
                        )
                    continue
                if field_values(back) != field_values(instance):
                    found.append(
                        Finding(
                            seed,
                            index,
                            f"published in {encoding}, arrived as {field_values(back)!r}, "
                            f"sent {field_values(instance)!r}",
                            described,
                            "other",
                        )
                    )
    return found


async def test_a_published_model_arrives_whole_or_is_refused():
    assert_only_known_limits(await findings(), [], check="P (publish)")


async def test_CONTROL_a_model_published_by_field_name_always_arrives_changed(monkeypatch):
    calls: list[object] = []

    def by_field_name(value):
        calls.append(value)
        return pydantic_core.to_jsonable_python(value, by_alias=False)

    monkeypatch.setattr(service_module, "wire_models", by_field_name)
    attempts: list[int] = []

    changed = [f for f in await findings(CONTROL_CASES, attempts) if f.detail == "other"]

    assert len(calls) == len(attempts) > 0, (
        "the replaced wire_models is not what every publish calls"
    )
    assert_control_finds(changed, at_least=CONTROL_FLOOR, control="always by field name")


class WrittenOnlyByAlias(BaseModel):
    """Its serializer refuses a dump that is not by alias."""

    n: int = Field(alias="nn")

    @field_serializer("n")
    def _by_alias_only(self, value: int, info: SerializationInfo) -> int:
        if not info.by_alias:
            raise ValueError("written by alias only")
        return value


async def test_a_model_whose_serializer_refuses_a_by_name_dump_is_published_by_alias():
    """The alias form reads back as the value, so it is chosen on the first validation; no
    by-name dump is attempted, and the serializer's refusal of one does not stop the publish."""
    service = _service()

    await service.publish_event("t.s", item=WrittenOnlyByAlias(nn=3))

    raw = service.nc.publish.await_args.args[1]
    sent = deserialize_payload(raw, content_type=None, fallback_format="json")["data"]["item"]
    assert sent == {"nn": 3}
    assert validate_decoded(WrittenOnlyByAlias, sent).n == 3
