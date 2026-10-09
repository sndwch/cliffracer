"""A handler parameter that declares a Pydantic alias is refused, naming the parameter.

`describe` and the generated client call a parameter by its Python name, and the payload model
the dispatcher validates against used the alias, so `Annotated[int, Field(alias="itemId")]` was
described as `item` and accepted only as `itemId`: a caller that followed the description was
refused with `missing`. The alias does nothing useful on a handler parameter, so it is refused
where the handler is built, for `@rpc` handlers, event listeners and broadcast handlers alike.
"""

from typing import Annotated

import pytest
from pydantic import AliasPath, Field, Strict

from cliffracer import CliffracerService, ServiceConfig, broadcast, listener, rpc
from cliffracer.core.typed_rpc import UntypedHandler
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit


def config(name: str) -> ServiceConfig:
    return ServiceConfig(name=name, subject_prefix=None, health_port=0)


def rpc_service(annotation):
    class Svc(CliffracerService):
        def __init__(self) -> None:
            super().__init__(config("svc"))

        @rpc
        async def take(self, item: annotation) -> int:  # type: ignore[valid-type]
            return 1

    return Svc


def listener_service(annotation):
    class Svc(CliffracerService):
        def __init__(self) -> None:
            super().__init__(config("svc"))

        @listener("things.created")
        async def on_created(self, subject: str, item: annotation) -> None:  # type: ignore[valid-type]
            return None

    return Svc


def broadcast_service(annotation):
    class Svc(CliffracerService):
        def __init__(self) -> None:
            super().__init__(config("svc"))

        @broadcast("things.created")
        async def on_created(self, subject: str, item: annotation) -> None:  # type: ignore[valid-type]
            return None

    return Svc


ALIASED = [
    Annotated[int, Field(alias="itemId")],
    Annotated[int, Field(validation_alias="itemId")],
    Annotated[int, Field(serialization_alias="itemId")],
    Annotated[int, Field(validation_alias=AliasPath("outer", "itemId"))],
    Annotated[int, Field(gt=0), Field(alias="itemId")],
]
IDS = ["alias", "validation_alias", "serialization_alias", "alias_path", "after_other_metadata"]


@pytest.mark.parametrize("annotation", ALIASED, ids=IDS)
def test_an_rpc_parameter_alias_is_refused_and_names_the_parameter(annotation):
    with pytest.raises(UntypedHandler) as caught:
        describe(rpc_service(annotation))

    message = str(caught.value)
    assert "Svc.take" in message and "parameter 'item'" in message, message
    assert "itemId" in message, message


@pytest.mark.parametrize("annotation", ALIASED, ids=IDS)
def test_the_service_refuses_to_discover_such_a_handler(annotation):
    with pytest.raises(UntypedHandler, match="parameter 'item'"):
        rpc_service(annotation)()._discover_handlers()


@pytest.mark.parametrize("annotation", ALIASED[:2], ids=IDS[:2])
def test_an_event_parameter_alias_is_refused_the_same_way(annotation):
    with pytest.raises(UntypedHandler) as caught:
        describe(listener_service(annotation))

    assert "Svc.on_created" in str(caught.value) and "parameter 'item'" in str(caught.value)


@pytest.mark.parametrize("annotation", ALIASED[:2], ids=IDS[:2])
def test_a_broadcast_parameter_alias_is_refused_the_same_way(annotation):
    with pytest.raises(UntypedHandler) as caught:
        describe(broadcast_service(annotation))

    assert "Svc.on_created" in str(caught.value) and "parameter 'item'" in str(caught.value)
    assert "itemId" in str(caught.value)


def test_CONTROL_a_broadcast_parameter_without_an_alias_describes():
    description = describe(broadcast_service(Annotated[int, Field(gt=0)]))

    assert [entry.handler_name for entry in description.listeners] == ["on_created"]


@pytest.mark.parametrize(
    "annotation",
    [
        Annotated[int, Field(gt=0)],
        Annotated[int, Field(description="how many")],
        # Metadata that is not a pydantic `Field` is passed over, not read for an alias.
        Annotated[int, "a note"],
        Annotated[int, Strict()],
    ],
    ids=["constraint", "description", "plain-metadata", "strict"],
)
def test_CONTROL_a_parameter_without_an_alias_describes_and_is_accepted(annotation):
    description = describe(rpc_service(annotation))

    assert [p.name for p in description.methods[0].params] == ["item"]
