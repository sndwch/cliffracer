"""Integration tests for the extension example from docs/extensions.md."""

import asyncio
import json
from pathlib import Path

import pytest

from cliffracer import CliffracerService, ServiceConfig, listener, rpc
from cliffracer.core.extension import Extension, RejectMessage, WorkerContext

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]

REPO = Path(__file__).resolve().parents[2]
DOC = REPO / "docs" / "extensions.md"
# Built by concatenation so these two lines do not themselves contain the
# markers: the first version split on the constant's own definition and found
# an empty example, which read as "the doc is fine" rather than as a bug.
BEGIN = "# --- BEGIN " + "docs/extensions.md worked example ---"
END = "# --- END " + "docs/extensions.md worked example ---"


# --- BEGIN docs/extensions.md worked example ---
class AuditExtension(Extension):
    """Publish one audit record per dispatch, and refuse unsigned messages.

    Declared on a service as a class attribute:

        class Orders(CliffracerService):
            audit = AuditExtension(require_signature=True)
    """

    def __init__(self, *, require_signature: bool = False) -> None:
        self.require_signature = require_signature
        # DECLARED here, CREATED in setup(). bind() is a shallow copy, so a
        # dict built in __init__ is the SAME object in every bound copy, and
        # two services would share each other's counts.
        self.counts: dict[str, int] | None = None

    async def setup(self, ctx) -> None:
        self.counts = {"ok": 0, "failed": 0, "refused": 0}

    async def worker_setup(self, ctx: WorkerContext) -> None:
        if self.require_signature and "x-signature" not in ctx.headers:
            # The ONE hook exception that stops the handler. Anything else
            # raised from any hook is logged under this extension's name and
            # swallowed, so a buggy hook cannot take dispatch down.
            self.counts["refused"] += 1
            raise RejectMessage("unsigned")
        # Hand state DOWN THE CHAIN, not onto self: ctx.data is per-dispatch,
        # and self is shared by every dispatch running concurrently.
        ctx.data["audit_started"] = True

    async def worker_result(self, ctx: WorkerContext, result, exc) -> None:
        if isinstance(exc, RejectMessage):
            return  # counted in worker_setup; a refusal is not a failure
        self.counts["failed" if exc is not None else "ok"] += 1

    async def worker_teardown(self, ctx: WorkerContext) -> None:
        if not ctx.data.get("audit_started"):
            return  # refused before the handler ran: nothing happened to audit
        await self.service.publish_event(
            f"audit.{self.service.config.name}",
            kind=ctx.kind,
            # `on_subject`, not `subject`: publish_event takes the subject
            # positionally, so a `subject=` keyword collides with it and raises
            # -- and a hook exception is swallowed, so it fails invisibly.
            on_subject=ctx.subject,
        )

    def health_details(self) -> dict | None:
        # None before setup(): "not set up yet" is not a fault worth reporting
        # on /health, and the containers do not exist to report on.
        return None if self.counts is None else dict(self.counts)


# --- END docs/extensions.md worked example ---


class Orders(CliffracerService):
    audit = AuditExtension(require_signature=True)

    @rpc
    async def place(self, item: str) -> dict[str, str]:
        return {"placed": item}

    @rpc
    async def explode(self) -> dict[str, str]:
        raise RuntimeError("boom")


class AuditWatcher(CliffracerService):
    def __init__(self, config, seen):
        super().__init__(config)
        self._seen = seen

    @listener("audit.orders", fanout=True)
    async def on_audit(
        self,
        subject: str,
        kind: str = "",
        on_subject: str = "",
    ) -> None:
        self._seen.append({"kind": kind, "on_subject": on_subject})


async def _until(predicate, timeout=5.0) -> bool:
    loop = asyncio.get_running_loop()
    deadline = loop.time() + timeout
    while loop.time() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.05)
    return False


async def _rpc(watcher, method: str, headers: dict | None = None, **kwargs) -> dict:
    """Invoke orders.rpc.<method> via nc.request with optional NATS headers."""
    reply = await watcher.nc.request(
        f"orders.rpc.{method}",
        json.dumps(kwargs).encode(),
        headers=headers,
        timeout=5,
    )
    return json.loads(reply.data.decode())


@pytest.fixture
async def running():
    seen: list[dict] = []
    watcher = AuditWatcher(ServiceConfig(name="audit_watcher"), seen)
    orders = Orders(ServiceConfig(name="orders", health_listener=False))
    await watcher.start()
    await orders.start()
    await asyncio.sleep(0.1)  # let the watcher's subscription land
    try:
        yield orders, watcher, seen
    finally:
        await orders.stop()
        await watcher.stop()


async def test_a_signed_call_runs_and_is_audited(running):
    orders, watcher, seen = running
    body = await _rpc(watcher, "place", {"x-signature": "abc"}, item="widget")

    assert body.get("result") == {"placed": "widget"}, body
    assert await _until(lambda: seen), "no audit record was published"
    assert seen[-1]["kind"] == "rpc"
    assert seen[-1]["on_subject"] == "orders.rpc.place"
    assert orders.audit.counts == {"ok": 1, "failed": 0, "refused": 0}


async def test_an_unsigned_call_is_refused_and_never_audited(running):
    orders, watcher, seen = running
    body = await _rpc(watcher, "place", None, item="widget")

    assert "result" not in body, body
    # worker_teardown returns early because worker_setup never set the flag: a
    # refused message must not appear in the audit trail as though it ran.
    assert not await _until(lambda: seen, timeout=1.0), f"refused call was audited: {seen}"
    assert orders.audit.counts == {"ok": 0, "failed": 0, "refused": 1}


async def test_a_handler_that_raises_is_counted_failed_not_ok(running):
    orders, watcher, seen = running
    body = await _rpc(watcher, "explode", {"x-signature": "abc"})

    assert "result" not in body, body
    assert orders.audit.counts == {"ok": 0, "failed": 1, "refused": 0}
    # A failure IS audited: it reached the handler, so something happened.
    assert await _until(lambda: seen), "a failed dispatch was not audited"


async def test_the_counts_reach_the_health_payload(running):
    orders, watcher, _ = running
    await _rpc(watcher, "place", {"x-signature": "abc"}, item="widget")

    health = await orders.health_check()
    assert health["audit"] == {"ok": 1, "failed": 0, "refused": 0}, health
