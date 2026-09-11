"""Adversarial live broker integration stress tests for authentication extension."""

import asyncio
import json

import nats
import pytest
from cliffracer_auth import (
    AuthConfig,
    AuthUser,
    get_current_user,
    requires_roles,
)
from cliffracer_auth.extension import AuthExtension
from cliffracer_auth.simple_auth import SimpleAuthService

from cliffracer import (
    CliffracerService,
    ServiceConfig,
    StreamSpec,
    listener,
    rpc,
    timer,
)
from tests.conftest import broker_url

SECRET = "live-test-secret-at-least-32-chars-long-9876543210"


@pytest.fixture(autouse=True)
async def _cleanup_streams():
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    for stream in ("LIVE82_EVENTS", "LIVE82_DLQ"):
        try:
            await js.delete_stream(stream)
        except Exception:
            pass
    await nc.close()
    yield
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    for stream in ("LIVE82_EVENTS", "LIVE82_DLQ"):
        try:
            await js.delete_stream(stream)
        except Exception:
            pass
    await nc.close()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_overlapping_listeners_burst():
    """Burst of 30 events published to overlapping subjects over live NATS.
    Verifies exact execution counts and zero duplicates under concurrent delivery.
    """
    counts = {
        "wc": 0,
        "created": 0,
        "gt": 0,
    }
    lock = asyncio.Lock()

    class BurstService(CliffracerService):
        @listener("burst.orders.*", fanout=True)
        async def on_wc(self, subject: str, order_id: int = 0) -> None:
            async with lock:
                counts["wc"] += 1

        @listener("burst.orders.created", fanout=True)
        async def on_created(self, subject: str, order_id: int = 0) -> None:
            async with lock:
                counts["created"] += 1

        @listener("burst.orders.>", fanout=True)
        async def on_gt(
            self,
            subject: str,
            order_id: int | None = None,
            eu_id: int | None = None,
        ) -> None:
            async with lock:
                counts["gt"] += 1

    svc = BurstService(ServiceConfig(name="live_burst_svc"))
    await svc.start()

    client_nc = await nats.connect(broker_url())
    try:
        # Publish 20 events on burst.orders.created -> matches wc, created, and gt (all 3)
        # Publish 10 events on burst.orders.eu.created -> matches gt only
        pub_tasks = []
        for i in range(20):
            pub_tasks.append(
                client_nc.publish("burst.orders.created", json.dumps({"order_id": i}).encode())
            )
        for i in range(10):
            pub_tasks.append(
                client_nc.publish("burst.orders.eu.created", json.dumps({"eu_id": i}).encode())
            )
        await asyncio.gather(*pub_tasks)
        await client_nc.flush()

        # Wait for all deliveries to process
        for _ in range(50):
            async with lock:
                if counts["wc"] == 20 and counts["created"] == 20 and counts["gt"] == 30:
                    break
            await asyncio.sleep(0.1)

        async with lock:
            assert counts["wc"] == 20, f"Expected 20, got {counts['wc']}"
            assert counts["created"] == 20, f"Expected 20, got {counts['created']}"
            assert counts["gt"] == 30, f"Expected 30, got {counts['gt']}"

    finally:
        await client_nc.close()
        await svc.stop()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_jetstream_pull_consumer_malformed_json_dlq():
    """Over live JetStream: a durable pull consumer receiving malformed JSON
    routes to DLQ and terminates, leaving 0 pending and 0 ack_pending.
    """
    dlq_messages = []

    class PullConsumerService(CliffracerService):
        @listener("live82.pull.events", durable="live82-puller", pull=True)
        async def on_pull_event(self, subject: str) -> None:
            pass

    cfg = ServiceConfig(
        name="live82_pull_svc",
        jetstream_enabled=True,
        jetstream_streams=[
            StreamSpec(name="LIVE82_EVENTS", subjects=["live82.pull.*"]),
            StreamSpec(name="LIVE82_DLQ", subjects=["dlq.*"]),
        ],
    )
    svc = PullConsumerService(cfg)
    await svc.start()

    raw_nc = await nats.connect(broker_url())
    raw_js = raw_nc.jetstream()

    try:

        async def on_dlq(msg):
            data = json.loads(msg.data.decode(errors="replace"))
            dlq_messages.append(data)

        await raw_nc.subscribe("dlq.live82_pull_svc", cb=on_dlq)
        await raw_nc.flush()

        # Publish malformed JSON to pull consumer stream
        await raw_js.publish(
            "live82.pull.events",
            b"{{malformed-json-here",
            headers={"Content-Type": "application/json"},
        )

        for _ in range(50):
            if len(dlq_messages) >= 1:
                break
            await asyncio.sleep(0.1)

        assert len(dlq_messages) == 1
        assert dlq_messages[0]["original_subject"] == "live82.pull.events"
        assert "Decode error" in dlq_messages[0]["error"]

        # Verify consumer state: no pending, no ack_pending, no poison loop
        info = await raw_js.consumer_info("LIVE82_EVENTS", "live82-puller")
        assert info.num_pending == 0
        assert info.num_ack_pending == 0

    finally:
        await raw_nc.close()
        await svc.stop()


@pytest.mark.integration
@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_auth_timer_concurrent_with_rpc():
    """Over live NATS: timer with default_timer_user runs authenticated in background,
    while RPC requires authentication and rejects unauthenticated callers.
    """
    timer_runs = 0
    bot_user = AuthUser(
        user_id="timer-bot-live",
        username="live_cron",
        email="cron@live.net",
        roles={"scheduler", "admin"},
    )

    auth_svc = SimpleAuthService(AuthConfig(secret_key=SECRET))
    auth_ext = AuthExtension(auth_svc, default_timer_user=bot_user, allow_timers=True)

    class AuthTimerLiveService(CliffracerService):
        auth = auth_ext

        @timer(interval=0.05)
        @requires_roles("scheduler")
        async def background_tick(self):
            nonlocal timer_runs
            user = get_current_user()
            if user and user.username == "live_cron":
                timer_runs += 1

        @rpc
        @requires_roles("admin")
        async def secure_rpc(self) -> str:
            return "authenticated_rpc_success"

    svc = AuthTimerLiveService(ServiceConfig(name="live_auth_svc"))
    await svc.start()

    client_nc = await nats.connect(broker_url())
    try:
        # 1. Verify timer runs repeatedly and successfully under role guard
        for _ in range(30):
            if timer_runs >= 2:
                break
            await asyncio.sleep(0.05)
        assert timer_runs >= 2

        # 2. Unauthenticated RPC call over wire MUST BE REJECTED
        resp = await client_nc.request(
            "live_auth_svc.rpc.secure_rpc",
            b"{}",
            timeout=5.0,
        )
        reply = json.loads(resp.data.decode())
        assert "refused: unauthenticated" in reply["error"]
        assert "unauthenticated" in reply["error"]

    finally:
        await client_nc.close()
        await svc.stop()
