"""A return model's contract hash is the hash of what the handler writes.

`type_ref` hashed every model by its validation-mode JSON Schema, which is what a caller may send.
For a return type that is the wrong schema: a `computed_field` adds a key to the reply and a
`serialization_alias` renames one, and neither changes the validation schema. A service that gained
a field in its replies kept the same `signature_hash` and the same description hash, so `verify`,
a template's contract check and a generated client's `--check` all saw no change. A return model is
hashed in serialization mode; a parameter model stays in validation mode.
"""

import json
from typing import Annotated

import pytest
from pydantic import BaseModel, Field, computed_field

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit


def parcel(kind: str = "plain") -> type[BaseModel]:
    """A model named `Parcel` in this module, in one of three shapes: the identity a change keeps."""
    if kind == "computed":

        class Parcel(BaseModel):
            count: int = 0

            @computed_field  # type: ignore[prop-decorator]
            @property
            def doubled(self) -> int:
                return self.count * 2

    elif kind == "aliased":

        class Parcel(BaseModel):  # type: ignore[no-redef]
            count: int = Field(0, serialization_alias="total")

    else:

        class Parcel(BaseModel):  # type: ignore[no-redef]
            count: int = 0

    Parcel.__qualname__ = "Parcel"
    return Parcel


def service(returns, takes=None):
    class Svc(CliffracerService):
        def __init__(self) -> None:
            super().__init__(ServiceConfig(name="svc", subject_prefix=None, health_port=0))

        if takes is None:

            @rpc
            async def get(self) -> returns:  # type: ignore[valid-type]
                return returns()

        else:

            @rpc
            async def get(self, parcel: takes) -> int:  # type: ignore[valid-type]
                return 1

    return Svc


def method_hash(cls) -> str:
    (method,) = describe(cls).methods
    return method.signature_hash


def test_a_computed_field_on_a_return_model_changes_the_signature_hash():
    plain, computed = parcel(), parcel("computed")

    assert method_hash(service(plain)) != method_hash(service(computed))


def test_a_serialization_alias_on_a_return_model_changes_the_signature_hash():
    plain, aliased = parcel(), parcel("aliased")

    assert method_hash(service(plain)) != method_hash(service(aliased))


WRAPPERS = [
    pytest.param(lambda model: list[model], id="list"),
    pytest.param(lambda model: model | None, id="optional"),
    pytest.param(lambda model: Annotated[model, "documented"], id="annotated"),
    pytest.param(lambda model: dict[str, model], id="dict"),
    pytest.param(lambda model: list[model] | None, id="optional-list"),
]


@pytest.mark.parametrize("wrap", WRAPPERS)
@pytest.mark.parametrize("kind", ["computed", "aliased"])
def test_a_change_to_a_return_model_inside_a_wrapper_changes_the_signature_hash(wrap, kind):
    """The model is reached through `list[...]`, `X | None`, `Annotated[...]` and `dict[str, ...]`."""
    plain, changed = parcel(), parcel(kind)

    assert method_hash(service(wrap(plain))) != method_hash(service(wrap(changed)))


@pytest.mark.parametrize("wrap", WRAPPERS)
def test_CONTROL_the_same_return_model_inside_a_wrapper_hashes_the_same(wrap):
    assert method_hash(service(wrap(parcel()))) == method_hash(service(wrap(parcel())))


@pytest.mark.parametrize("wrap", WRAPPERS)
def test_CONTROL_a_computed_field_on_a_PARAMETER_model_inside_a_wrapper_does_not_change_the_hash(
    wrap,
):
    plain, computed = parcel(), parcel("computed")

    assert method_hash(service(None, takes=wrap(plain))) == method_hash(
        service(None, takes=wrap(computed))
    )


def test_a_computed_field_on_a_return_model_changes_the_description_hash():
    plain, computed = parcel(), parcel("computed")

    assert describe(service(plain)).description_hash != describe(service(computed)).description_hash


def test_CONTROL_the_same_return_model_hashes_the_same():
    assert method_hash(service(parcel())) == method_hash(service(parcel()))


def test_CONTROL_a_computed_field_on_a_PARAMETER_model_does_not_change_the_hash():
    """A parameter is hashed by what a caller may send, and a computed field is never sent."""
    plain, computed = parcel(), parcel("computed")

    assert method_hash(service(None, takes=plain)) == method_hash(service(None, takes=computed))


@pytest.mark.parametrize("wrap", [pytest.param(lambda model: model, id="bare"), *WRAPPERS])
def test_the_components_hold_the_serialization_schema_of_a_return_model(wrap):
    """The return model is collected in serialization mode, bare and inside every wrapper the
    hash tests use, so the components hold the computed field."""
    computed = parcel("computed")

    components = describe(service(wrap(computed))).components

    assert any("doubled" in json.dumps(schema) for schema in components.values()), components
