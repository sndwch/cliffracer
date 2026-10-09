"""A message is judged by the service's declared extensions before its payload is validated.

`ValidationExtension` was bound before every extension a service declares. For an RPC with an invalid
payload it refused the message first, so an unauthenticated caller was told `validation_failed` with the
schema's field names, messages and the rejected input instead of `refused: unauthenticated`, a rate
limit or policy check never ran, and the service's own pydantic validators ran on input a gate would
have turned away. It is now bound last: a gate refuses before any schema diagnostic is produced, and
validation still refuses a payload the gates admitted.
"""

import json

import pytest

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


class Gate(Extension):
    """A refusing extension, as a rate limit or a policy check is."""

    def __init__(self) -> None:
        self.seen: list[str] = []

    async def worker_setup(self, ctx):
        self.seen.append(ctx.kind)
        raise RejectMessage("over quota")


class Observer(Extension):
    """Admits everything and records what it can read at each hook."""

    def __init__(self) -> None:
        self.setup_saw: list[dict] = []
        self.result_saw: list[dict] = []

    async def worker_setup(self, ctx):
        self.setup_saw.append(
            {"payload": dict(ctx.payload), "validated": "validated_kwargs" in ctx.data}
        )

    async def worker_result(self, ctx, result, exc):
        self.result_saw.append({"validated": ctx.data.get("validated_kwargs"), "exc": exc})


def _service(*extensions: tuple[str, type[Extension]]) -> type[CliffracerService]:
    namespace: dict = {name: factory() for name, factory in extensions}

    @rpc
    async def transfer(self, account: str, amount: int) -> str:
        return "moved"

    namespace["transfer"] = transfer
    return type("Bank", (CliffracerService,), namespace)


def _harness(cls) -> ServiceTestHarness:
    return ServiceTestHarness(cls, config=ServiceConfig(name="bank", health_port=0))


VALID = {"account": "a", "amount": 5}
INVALID = {"account": "a", "amount": "lots", "secret_field": 1}


def _extension(harness, name):
    return next(e for e in harness.container.extensions if e.name == name)


async def test_a_refusing_gate_answers_an_invalid_payload_before_any_schema_diagnostic():
    async with _harness(_service(("gate", Gate))) as harness:
        reply = (await harness.rpc("transfer", payload=INVALID)).data

        assert reply["code"] == "refused" and reply["error"] == "refused: over quota", reply
        assert "details" not in reply
        assert _extension(harness, "gate").seen == ["rpc"]


async def test_an_invalid_payload_is_refused_by_validation_when_the_gates_admit_it():
    async with _harness(_service(("observer", Observer))) as harness:
        reply = (await harness.rpc("transfer", payload=INVALID)).data

    assert reply["code"] == "validation_failed"
    assert json.dumps(reply["details"]).count("amount") >= 1


async def test_the_declared_extensions_run_before_validation_in_the_chain():
    async with _harness(_service(("observer", Observer), ("gate", Gate))) as harness:
        names = [e.name for e in harness.container.extensions]

    assert names == ["_correlation", "observer", "gate", "_validation"]


def test_the_service_binds_validation_after_the_extensions_it_declares(monkeypatch):
    """The order the service asks for, apart from the order the container ends up with.

    `Container._bind_extension` keeps `_validation` last whatever order it is bound in, so a
    service that bound it second would still end with it last: the list above cannot see that the
    intent in `_collect_extensions` was lost, and the next change to the container would then leave
    validation where the service put it. This reads the order of the binds the service makes.
    """
    from cliffracer.core.container import Container

    asked: list[str] = []
    bind = Container._bind_extension

    def recording(self, ext, name):
        asked.append(name)
        return bind(self, ext, name)

    monkeypatch.setattr(Container, "_bind_extension", recording)

    _service(("observer", Observer), ("gate", Gate))(ServiceConfig(name="bank", health_port=0))

    assert asked == ["_correlation", "observer", "gate", "_validation"]


async def test_an_extension_added_after_construction_still_runs_before_validation():
    cls = _service(("observer", Observer))
    service = cls(ServiceConfig(name="bank", health_port=0))

    service.add_extension(Gate(), "latecomer")

    assert [e.name for e in service.extensions] == [
        "_correlation",
        "observer",
        "latecomer",
        "_validation",
    ]


async def test_a_declared_setup_reads_the_payload_as_it_arrived_and_a_declared_result_reads_the_kwargs():
    async with _harness(_service(("observer", Observer))) as harness:
        reply = (await harness.rpc("transfer", payload=VALID)).data
        observer = _extension(harness, "observer")

    assert reply["result"] == "moved"
    (setup,) = observer.setup_saw
    assert setup["payload"]["amount"] == 5 and setup["validated"] is False
    (result,) = observer.result_saw
    assert result["validated"] == {"account": "a", "amount": 5}


async def test_CONTROL_a_valid_payload_through_an_admitting_chain_reaches_the_handler():
    async with _harness(_service(("observer", Observer))) as harness:
        reply = (await harness.rpc("transfer", payload=VALID)).data

    assert reply == {**reply, "success": True, "result": "moved"}


async def test_CONTROL_a_refusing_gate_refuses_a_valid_payload_too():
    async with _harness(_service(("gate", Gate))) as harness:
        reply = (await harness.rpc("transfer", payload=VALID)).data

    assert reply["code"] == "refused"
