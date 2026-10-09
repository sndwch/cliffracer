"""A model whose JSON dump fails is refused before it is sent, naming where it fails.

`wire_models` writes each model a call, a publish or a broadcast carries. When the dump itself fails
(bytes that are not UTF-8, an object JSON cannot write, a computed field that raises) the call is
refused by `RpcValidationError`, as a value that would be lost is, with a `value_cannot_be_written`
detail at the field that fails, followed into a nested model or a container, the dump's own error
as the cause, and none of the value in the message.
"""

from __future__ import annotations

from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict, computed_field
from pydantic_core import PydanticSerializationError

from cliffracer.core.exceptions import RpcValidationError
from cliffracer.core.validation import wire_models

pytestmark = pytest.mark.unit


class _Opaque:
    pass


class _Blob(BaseModel):
    note: str = "n"
    blob: bytes


class _HoldsAnything(BaseModel):
    model_config = ConfigDict(arbitrary_types_allowed=True)
    thing: Any


class _RaisingProperty(BaseModel):
    n: int = 0

    @computed_field  # type: ignore[prop-decorator]
    @property
    def boom(self) -> int:
        raise RuntimeError("the property failed")


class _HoldsABlob(BaseModel):
    inner: _Blob


class _BlobsInAList(BaseModel):
    blobs: list[_Blob]


@pytest.mark.parametrize(
    ("make", "model", "where", "cause"),
    [
        pytest.param(
            lambda: _Blob.model_construct(blob=b"\x00\xff"),
            "_Blob",
            "payload['blob']",
            UnicodeDecodeError,
            id="bytes-that-are-not-utf8",
        ),
        pytest.param(
            lambda: _HoldsAnything(thing=_Opaque()),
            "_HoldsAnything",
            "payload['thing']",
            PydanticSerializationError,
            id="an-any-holding-an-object",
        ),
        pytest.param(
            lambda: _RaisingProperty(),
            "_RaisingProperty",
            "payload['boom']",
            RuntimeError,
            id="a-computed-field-that-raises",
        ),
        pytest.param(
            lambda: _HoldsABlob(inner=_Blob.model_construct(blob=b"\xff")),
            "_Blob",
            "payload['inner']['blob']",
            UnicodeDecodeError,
            id="in-a-nested-model",
        ),
        pytest.param(
            lambda: _BlobsInAList(blobs=[_Blob(blob=b"ok"), _Blob.model_construct(blob=b"\xff")]),
            "_Blob",
            "payload['blobs'][1]['blob']",
            UnicodeDecodeError,
            id="in-a-list-field",
        ),
        pytest.param(
            lambda: {"order": _Blob.model_construct(blob=b"\xff")},
            "_Blob",
            "payload['order']['blob']",
            UnicodeDecodeError,
            id="a-model-in-a-calls-parameters",
        ),
    ],
)
def test_a_model_that_cannot_be_written_as_json_is_refused_by_name(make, model, where, cause):
    with pytest.raises(RpcValidationError) as refused:
        wire_models(make())

    message = str(refused.value)
    assert message.startswith("refused before sending"), message
    assert where in message and model in message, message
    assert [(d["type"], d["loc"]) for d in refused.value.details] == [
        ("value_cannot_be_written", [where])
    ]
    assert isinstance(refused.value.__cause__, cause), repr(refused.value.__cause__)
    assert "\\xff" not in message and "the property failed" not in message, message


def test_a_model_that_can_be_written_is_sent_as_before():
    """The refusal is for a dump that fails, not for every bytes field."""
    assert wire_models({"order": _Blob(blob=b"abc")}) == {"order": {"note": "n", "blob": "abc"}}
