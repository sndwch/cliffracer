"""Continuous benchmark tests for Core JetStream batch pull consumption."""

from __future__ import annotations

import pytest

from tests.benchmark.benchmarks import DEFAULT_NATS_URL, benchmark_jetstream

pytestmark = pytest.mark.benchmark


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_jetstream_batch_pull_benchmarks():
    """Verify JetStream batch pull consumer achieves high throughput and low batch fetch latency."""
    metrics = await benchmark_jetstream(DEFAULT_NATS_URL, total_messages=20000, batch_size=250)

    assert metrics["messages_consumed"] == 20000
    assert metrics["messages_acked"] == 20000
    assert metrics["throughput_msgs_sec"] > 500.0
    assert metrics["p50_batch_latency_ms"] > 0


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_CONTROL_messages_that_are_never_acked_are_not_reported_as_acked(monkeypatch):
    """Break acking and the harness must report the shortfall.

    `messages_acked` has to come from the server's ack floor, read back through
    `consumer_info`, rather than from the harness counting its own calls. This
    turns `Msg.ack()` into a no-op: the messages are still delivered, so
    `messages_consumed` is unchanged, and the acks never happen, so the ack
    floor stays behind. A harness counting its own calls would report both
    numbers equal and never notice.

    Replacing the previous control, which asked the harness for a partial
    consumption through a `max_consume` argument that existed for no other
    caller. Simulating a shortfall through a parameter is not the same as
    causing one.
    """
    import nats.aio.msg

    async def no_ack(self) -> None:
        return None

    monkeypatch.setattr(nats.aio.msg.Msg, "ack", no_ack, raising=True)

    metrics = await benchmark_jetstream(DEFAULT_NATS_URL, total_messages=500, batch_size=250)

    assert metrics["messages_consumed"] == 500, "delivery is unaffected by the broken ack"
    assert metrics["messages_acked"] < metrics["messages_consumed"], (
        f"acked {metrics['messages_acked']} of {metrics['messages_consumed']} with ack() "
        "disabled; the count is not being read from the server"
    )
