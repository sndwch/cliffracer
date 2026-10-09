"""A gate that crashes is an error in the dispatch counts, not a refusal.

The pipeline answers a `fails_closed` extension whose hook raises (an auth backend that is down, a
validator that raises) with a refusal marked `hook_crash`, and the wire reports it as `internal`: the
service is broken, the caller was not turned away. `MetricsExtension` counted every `RejectMessage` as
`rejected`, so an auth outage showed as a rise in `rejected` with `errors` at zero. `rejected` now counts
the refusals an extension authored, and the crash is an error.
"""

from typing import Annotated

import pytest
from cliffracer_metrics import MetricsExtension
from pydantic import AfterValidator

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext
from cliffracer.testing import ServiceTestHarness

pytestmark = pytest.mark.unit


class BrokenGate(Extension):
    """A gate whose backend is down."""

    fails_closed = True

    async def worker_setup(self, ctx):
        raise ConnectionError("auth backend unreachable")


class Policy(Extension):
    async def worker_setup(self, ctx):
        raise RejectMessage("not for you")


def _raises_a_type_error(value: int) -> int:
    raise TypeError("a validator with a bug")


def _service(gate: type[Extension] | None):
    namespace: dict = {"metrics": MetricsExtension()}
    if gate is not None:
        namespace["gate"] = gate()

    @rpc
    async def ping(self) -> str:
        return "pong"

    @rpc
    async def parse(self, n: Annotated[int, AfterValidator(_raises_a_type_error)]) -> int:
        return n

    namespace.update(ping=ping, parse=parse)
    return type("Svc", (CliffracerService,), namespace)


async def _counts(gate, method: str, **payload) -> tuple[str, dict]:
    async with ServiceTestHarness(
        _service(gate), config=ServiceConfig(name="m", health_port=0)
    ) as harness:
        reply = (await harness.rpc(method, payload=payload or None)).data
        return reply.get("code"), (await harness.service.health_check())["metrics"]["rpc"]


async def test_a_gate_that_crashes_is_counted_as_an_error():
    code, counts = await _counts(BrokenGate, "ping")

    assert code == "internal"
    assert (counts["errors"], counts["rejected"]) == (1, 0)


async def test_a_validator_that_crashes_is_counted_as_an_error():
    code, counts = await _counts(None, "parse", n=1)

    assert code == "internal"
    assert (counts["errors"], counts["rejected"]) == (1, 0)


async def test_CONTROL_a_refusal_an_extension_authored_is_still_rejected_not_an_error():
    code, counts = await _counts(Policy, "ping")

    assert code == "refused"
    assert (counts["errors"], counts["rejected"]) == (0, 1)


async def test_CONTROL_a_handler_exception_is_still_an_error():
    class Svc(CliffracerService):
        metrics = MetricsExtension()

        @rpc
        async def boom(self) -> str:
            raise RuntimeError("handler exploded")

    async with ServiceTestHarness(Svc, config=ServiceConfig(name="m", health_port=0)) as harness:
        await harness.rpc("boom")
        counts = (await harness.service.health_check())["metrics"]["rpc"]

    assert (counts["errors"], counts["rejected"]) == (1, 0)


async def test_the_extension_reads_the_hook_crash_flag_directly():
    metrics = MetricsExtension()
    await metrics.setup(None)  # type: ignore[arg-type]
    ctx = WorkerContext(kind="rpc", subject="s", headers={}, correlation_id="c", payload={})

    await metrics.worker_result(ctx, None, RejectMessage("backend down", hook_crash=True))
    await metrics.worker_result(ctx, None, RejectMessage("no"))

    out = metrics.health_details()["rpc"]
    assert (out["errors"], out["rejected"], out["count"]) == (1, 1, 2)
