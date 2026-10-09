"""A handler's return that streams is described as a stream, and a client is generated for it.

A return annotated `AsyncIterator[X]` or `AsyncGenerator[X, None]` is the TypeRef
`{"kind": "stream", "item": <X>}`. It is a handler's whole return, so a stream nested in another
type is still refused, and the kind sits in `returns`, so it is part of the signature hash: a
client generated for a reply of `X`, or of `list[X]`, fails `verify()` against a stream of `X`.
The generator writes such a method as an async generator over `ServiceClient._stream`, and refuses
a kind it does not know by name.

A service refuses, by name, a handler annotated as a stream that is not an async generator, and
reads a handler under a `functools.wraps` decorator as the function it wraps: a wrapped generator
is refused as one, and a wrapped async generator annotated as a stream is served as a stream.
"""

from __future__ import annotations

import functools
import inspect
import subprocess
import sys
import typing
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Annotated, Any

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.typed_rpc import (
    UnsupportedType,
    UntypedHandler,
    collect_model_schemas,
    return_type_ref,
    type_ref,
    unimportable_models,
)
from cliffracer.generate_client.emitter import CannotEmit, emit, imports_for
from cliffracer.introspect import Description, Method, Param, _signature_hash, describe

pytestmark = pytest.mark.unit

INT = {"kind": "scalar", "name": "int"}


class Item(BaseModel):
    n: int


# --- the TypeRef ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    "annotation",
    [
        pytest.param(AsyncIterator[int], id="asynciterator"),
        pytest.param(AsyncGenerator[int], id="asyncgenerator-one-argument"),
        # Both two-argument spellings: the `collections.abc` one holds the literal `None`, the
        # `typing` one `NoneType`. `UP043` would shorten them to the one-argument form above.
        pytest.param(AsyncGenerator[int, None], id="asyncgenerator-abc-none"),  # noqa: UP043
        pytest.param(typing.AsyncGenerator[int, None], id="asyncgenerator-typing-none"),  # noqa: UP043, UP006
        pytest.param(Annotated[AsyncIterator[int], "doc"], id="annotated"),
    ],
)
def test_a_streaming_return_is_a_stream_of_its_item(annotation):
    assert return_type_ref(annotation) == {"kind": "stream", "item": INT}


def test_a_stream_of_models_names_the_model_as_a_return_is_read():
    ref = return_type_ref(AsyncIterator[Item])

    assert ref == {"kind": "stream", "item": type_ref(Item, mode="serialization")}
    assert "Item" in collect_model_schemas(AsyncIterator[Item], mode="serialization").__repr__()


def test_CONTROL_a_return_that_does_not_stream_is_read_as_before():
    assert return_type_ref(list[int]) == type_ref(list[int], mode="serialization")


@pytest.mark.parametrize(
    ("annotation", "said"),
    [
        pytest.param(AsyncGenerator[int, str], "with a send type is unsupported", id="send-type"),
        pytest.param(AsyncIterator, "without an item type is unsupported", id="bare"),
    ],
)
def test_a_stream_annotation_the_wire_cannot_carry_is_refused_by_name(annotation, said):
    with pytest.raises(UnsupportedType, match=said):
        return_type_ref(annotation)


def test_a_stream_nested_in_another_type_is_refused():
    with pytest.raises(UnsupportedType, match="AsyncIterator is unsupported"):
        type_ref(list[AsyncIterator[int]], mode="serialization")


def test_the_signature_hash_tells_a_stream_from_its_item_and_from_a_list_of_it():
    params = [Param(name="n", type=INT)]
    hashes = {
        _signature_hash(params, {"kind": "stream", "item": INT}),
        _signature_hash(params, INT),
        _signature_hash(params, {"kind": "list", "item": INT}),
    }

    assert len(hashes) == 3


def test_an_unimportable_model_in_a_stream_is_named():
    ref = {"kind": "stream", "item": {"kind": "model", "module": "__main__", "qualname": "Item"}}

    assert unimportable_models(ref) == ["__main__:Item"]


# --- the generated client -------------------------------------------------------------------


def _streaming(item: dict[str, Any]) -> Description:
    returns = {"kind": "stream", "item": item}
    params = [Param(name="n", type=INT)]
    method = Method(
        name="tail",
        doc="",
        params=params,
        returns=returns,
        signature_hash=_signature_hash(params, returns),
    )
    return Description(service="logs", version="1.0.0", methods=[method])


def test_a_streaming_method_is_generated_as_an_async_generator_over_stream():
    source = emit(_streaming(INT))

    assert "    async def tail(self, n: int) -> _typing.AsyncIterator[int]:" in source, source
    assert "        _items = self._stream(" in source, source
    assert "        async with _contextlib.aclosing(_items):" in source, source
    assert "            async for _item in _items:" in source, source
    assert "                yield _typing.cast(_Return_tail, _item)" in source, source
    assert "import contextlib as _contextlib" in source, source
    assert "type _Return_tail = int" in source, source
    assert "await self._call(" not in source, source


@pytest.mark.parametrize(
    "name", ["tail", "a_rather_long_streaming_method_name_for_wrapping"], ids=["short", "long"]
)
def test_the_generated_streaming_method_needs_no_reformatting(name, tmp_path):
    """Ruff's own default line length applies in a directory with no configuration of its own."""
    returns = {"kind": "stream", "item": INT}
    params = [Param(name="n", type=INT)]
    method = Method(
        name=name,
        doc="",
        params=params,
        returns=returns,
        signature_hash=_signature_hash(params, returns),
    )
    desc = Description(
        service="logs", version="1.0.0", description_hash="sha256:0", methods=[method]
    )
    path = tmp_path / "client.py"
    path.write_text(emit(desc))

    done = subprocess.run(
        [sys.executable, "-m", "ruff", "format", "--check", str(path)],
        capture_output=True,
        text=True,
        cwd=str(tmp_path),
    )

    assert done.returncode == 0, done.stdout + done.stderr


def test_the_generated_streaming_method_is_an_async_generator():
    namespace: dict[str, Any] = {}
    exec(compile(emit(_streaming(INT)), "<generated>", "exec"), namespace)  # noqa: S102

    client = next(v for v in namespace.values() if isinstance(v, type) and hasattr(v, "tail"))
    assert inspect.isasyncgenfunction(client.tail)


def test_a_streams_item_model_is_imported_by_the_client():
    item = type_ref(Item, mode="serialization")

    assert (Item.__module__, "Item") in imports_for(_streaming(item))


def test_a_stream_of_literals_imports_literal():
    source = emit(_streaming({"kind": "literal", "values": ["a", "b"]}))

    assert "from typing import Literal" in source, source


def test_a_kind_the_generator_does_not_know_is_refused_by_name():
    with pytest.raises(CannotEmit, match="unknown TypeRef kind 'chunks'"):
        emit(
            _streaming(INT).__class__(
                service="logs",
                version="1.0.0",
                methods=[
                    Method(name="tail", doc="", params=[], returns={"kind": "chunks", "item": INT})
                ],
            )
        )


# --- what a service refuses now -------------------------------------------------------------


def _service(handler) -> type[CliffracerService]:
    def __init__(self):
        CliffracerService.__init__(
            self, ServiceConfig(name="svc", subject_prefix=None, health_port=0)
        )

    handler.__name__ = "tail"
    return type("Svc", (CliffracerService,), {"__init__": __init__, "tail": rpc(handler)})


async def returns_an_iterator(self, n: int) -> AsyncIterator[int]:
    raise NotImplementedError


def keeps_its_name(func):
    @functools.wraps(func)
    def wrapper(*args, **kwargs):
        return func(*args, **kwargs)

    return wrapper


@keeps_its_name
async def a_wrapped_async_generator(self, n: int) -> AsyncIterator[int]:
    for i in range(n):
        yield i


@keeps_its_name
async def a_wrapped_async_generator_returning_int(self, n: int) -> int:  # type: ignore[misc]
    for i in range(n):
        yield i


def _refusal(handler) -> str:
    cls = _service(handler)
    service = cls()
    with pytest.raises(UntypedHandler) as discovered:
        HandlerDiscovery.discover(service, service.config)
    with pytest.raises(UntypedHandler) as described:
        describe(cls)
    assert str(discovered.value) == str(described.value)
    return str(discovered.value)


def test_a_handler_annotated_as_a_stream_that_is_not_an_async_generator_is_refused_by_name():
    said = _refusal(returns_an_iterator)

    assert said.startswith(
        "Svc.tail: return: annotated AsyncIterator, a stream, but the "
        "handler is not an async generator"
    ), said


def test_a_generator_under_a_wrapping_decorator_is_refused_as_a_generator():
    said = _refusal(a_wrapped_async_generator_returning_int)

    assert said.startswith("Svc.tail: an RPC handler cannot be a generator"), said


def test_an_async_generator_under_a_wrapping_decorator_annotated_as_a_stream_is_one():
    cls = _service(a_wrapped_async_generator)
    service = cls()

    registry = HandlerDiscovery.discover(service, service.config)

    assert registry.rpc_specs["tail"].streams
    assert [m.returns for m in describe(cls).methods] == [{"kind": "stream", "item": INT}]
