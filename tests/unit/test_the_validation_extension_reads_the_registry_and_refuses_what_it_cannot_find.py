"""The validation extension reads the registry the dispatcher reads, and fails closed on a miss.

It looked for a handler's spec in three places on the service object, two of which never
exist for a core service, and a miss returned without a word: the dispatcher then handed the
handler the raw wire payload, from the one extension that declares `fails_closed`. It reads
`container.registry.rpc_specs`, the table the dispatcher indexes, and a named handler with no
spec raises.

It also leaves `ctx.payload` as it arrived and puts the validated, coerced arguments in
`ctx.data["validated_kwargs"]`, which `docs/extensions.md` now says; that is pinned here.
"""

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import RejectMessage, WorkerContext
from cliffracer.core.validation_extension import ValidationExtension

pytestmark = pytest.mark.unit


class Svc(CliffracerService):
    @rpc
    async def add(self, amount: int, label: str = "x") -> int:
        return amount


def _setup():
    svc = Svc(ServiceConfig(name="validated"))
    svc._discover_handlers()
    ext = next(e for e in svc.container.extensions if isinstance(e, ValidationExtension))
    return svc, ext


def _ctx(payload, handler_name="add", kind="rpc"):
    data = {} if handler_name is None else {"handler_name": handler_name}
    return WorkerContext(
        kind=kind, subject=None, headers={}, correlation_id=None, payload=payload, data=data
    )


async def test_a_named_handler_with_no_spec_is_refused_not_dispatched_unvalidated():
    _, ext = _setup()

    with pytest.raises(RuntimeError, match="'ghost'"):
        await ext.worker_setup(_ctx({"amount": 1}, handler_name="ghost"))


async def test_the_spec_is_read_from_the_registry_the_dispatcher_reads():
    svc, ext = _setup()
    del svc.container.registry.rpc_specs["add"]

    with pytest.raises(RuntimeError, match="'add'"):
        await ext.worker_setup(_ctx({"amount": 1}))


async def test_CONTROL_a_context_that_names_no_handler_has_nothing_to_validate():
    _, ext = _setup()

    await ext.worker_setup(_ctx({"amount": 1}, handler_name=None))


async def test_CONTROL_another_kind_of_dispatch_is_left_alone():
    _, ext = _setup()

    await ext.worker_setup(_ctx({"amount": 1}, handler_name="ghost", kind="event"))


async def test_validated_arguments_are_in_data_and_the_payload_is_left_as_it_arrived():
    _, ext = _setup()
    ctx = _ctx({"amount": "5", "correlation_id": "c-1"})

    await ext.worker_setup(ctx)

    assert ctx.data["validated_kwargs"] == {"amount": 5, "label": "x"}
    assert ctx.payload == {"amount": "5", "correlation_id": "c-1"}


async def test_CONTROL_a_payload_that_does_not_validate_is_still_rejected():
    _, ext = _setup()
    ctx = _ctx({"amount": "not a number"})

    with pytest.raises(RejectMessage):
        await ext.worker_setup(ctx)

    assert "validation_error" in ctx.data
