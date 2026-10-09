"""An RPC payload that is any `Mapping`, not only a `dict`, is validated as the object it is.

`ValidationExtension` removed the message's `correlation_id` only from a `dict`, and refused a
payload that was not one with "validation failed: payload must be an object" after validating it
successfully: a reply with no field errors, unlike every other refusal. The refusal was reachable
only by a `Mapping` that is not a `dict` (a frozen copy of the payload, a `UserDict`), since a
model built from a handler's parameters refuses every other value; and for such a payload that
carried a `correlation_id` the refusal was different again, the id being refused as an extra field.
The wire only delivers a `dict`, so this is read through the extension, as an earlier extension or
an in-process caller would leave the payload.
"""

from __future__ import annotations

from collections import OrderedDict, UserDict
from collections.abc import Mapping
from types import MappingProxyType
from typing import Any

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import RejectMessage, WorkerContext

pytestmark = pytest.mark.unit


class Svc(CliffracerService):
    @rpc
    async def echo(self, x: int, y: str = "d") -> dict[str, int]:
        return {"x": x}

    @rpc
    async def nothing(self) -> dict[str, int]:
        return {}


class _Frozen(Mapping):
    """A `Mapping` that is neither a `dict` nor a `UserDict`."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = dict(data)

    def __getitem__(self, key):
        return self._data[key]

    def __iter__(self):
        return iter(self._data)

    def __len__(self) -> int:
        return len(self._data)


MAPPINGS = [MappingProxyType, UserDict, _Frozen]
MAPPING_IDS = ["mappingproxy", "userdict", "custom-mapping"]


async def _read(
    handler: str, payload: Any, **config: Any
) -> tuple[WorkerContext, Exception | None]:
    svc = Svc(ServiceConfig(name="s", health_port=0, **config))
    await svc.container._setup_extensions()
    svc._discover_handlers()
    extension = next(
        e for e in svc.container.extensions if type(e).__name__ == "ValidationExtension"
    )
    ctx = WorkerContext(
        kind="rpc",
        subject=f"s.rpc.{handler}",
        headers={},
        correlation_id=None,
        payload=payload,
        raw=None,
    )
    ctx.data["handler_name"] = handler
    try:
        await extension.worker_setup(ctx)
    except RejectMessage as refusal:
        return ctx, refusal
    return ctx, None


@pytest.mark.parametrize("make", MAPPINGS, ids=MAPPING_IDS)
async def test_a_mapping_that_is_not_a_dict_is_served_like_a_dict(make):
    ctx, refusal = await _read("echo", make({"x": 1}))

    assert refusal is None
    assert ctx.data["validated_kwargs"] == {"x": 1, "y": "d"}


@pytest.mark.parametrize("make", MAPPINGS, ids=MAPPING_IDS)
async def test_a_mapping_carrying_the_correlation_id_has_it_removed_as_a_dict_does(make):
    ctx, refusal = await _read("echo", make({"x": 1, "correlation_id": "c-1"}))

    assert refusal is None, f"refused: {refusal}; {ctx.data.get('validation_error')}"
    assert ctx.data["validated_kwargs"] == {"x": 1, "y": "d"}


async def test_a_mapping_for_a_handler_with_no_parameters_is_served():
    ctx, refusal = await _read("nothing", MappingProxyType({}))

    assert refusal is None
    assert ctx.data["validated_kwargs"] == {}


@pytest.mark.parametrize("make", MAPPINGS, ids=MAPPING_IDS)
async def test_a_mapping_that_is_invalid_is_refused_like_an_invalid_dict(make):
    ctx, refusal = await _read("echo", make({"x": "not a number"}))

    assert str(refusal) == "validation failed"
    assert ctx.data["validation_error"].errors()[0]["loc"] == ("x",)


@pytest.mark.parametrize("payload", [42, "hello", [1, 2], None, True], ids=repr)
async def test_a_payload_that_is_not_an_object_is_refused_with_its_field_errors(payload):
    """The same refusal as any other invalid payload, never a reply with no details."""
    ctx, refusal = await _read("echo", payload)

    assert str(refusal) == "validation failed"
    assert ctx.data["validation_error"].errors(), "a refusal that names nothing"


@pytest.mark.parametrize("payload", [42, [], None], ids=repr)
async def test_a_payload_that_is_not_an_object_is_refused_for_a_handler_with_no_parameters(payload):
    ctx, refusal = await _read("nothing", payload)

    assert str(refusal) == "validation failed"
    assert "validation_error" in ctx.data


@pytest.mark.parametrize("make", [dict, OrderedDict], ids=["dict", "ordereddict"])
async def test_CONTROL_a_dict_and_a_dict_subclass_are_served(make):
    ctx, refusal = await _read("echo", make({"x": 2, "correlation_id": "c-1"}))

    assert refusal is None
    assert ctx.data["validated_kwargs"] == {"x": 2, "y": "d"}


async def test_a_mapping_is_served_when_validation_errors_are_redacted():
    ctx, refusal = await _read("echo", MappingProxyType({"x": 1}), rpc_validation_errors="redacted")

    assert refusal is None
    assert ctx.data["validated_kwargs"] == {"x": 1, "y": "d"}


async def test_an_invalid_mapping_is_refused_redacted_when_validation_errors_are_redacted():
    ctx, refusal = await _read(
        "echo", MappingProxyType({"x": "secret-value"}), rpc_validation_errors="redacted"
    )

    assert str(refusal) == "validation failed"
    assert "secret-value" not in repr(ctx.data["validation_error"].errors())
