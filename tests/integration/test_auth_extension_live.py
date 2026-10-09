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
from cliffracer.core.discovery import HandlerDiscovery
from cliffracer.core.jetstream import all_streams
from cliffracer.testing.waiting import wait_until
from tests.broker_isolation import prefixed_name
from tests.conftest import broker_url

pytestmark = pytest.mark.integration

SECRET = "live-test-secret-at-least-32-chars-long-9876543210"


#: The streams this module declares. Bare here, as the specs are written; what
#: the broker holds is the prefixed form, which is the whole point below.
LIVE82_STREAMS = ("LIVE82_EVENTS", "LIVE82_DLQ")


async def _this_modules_streams(js) -> list[str]:
    """The streams to delete: this module's specs, named as the broker holds them.

    The listing is paged -- `streams_info()` answers one page, and this tier has
    run against a broker holding hundreds. `all_streams` is the one reader that
    knows how to finish a listing; a local loop here would be a second
    termination rule for the same question.
    """
    wanted = {prefixed_name(name) for name in LIVE82_STREAMS}
    return [i.config.name for i in await all_streams(js) if i.config.name in wanted]


async def _every_live82_stream(js) -> set[str]:
    """Every stream on the broker carrying this module's marker, whoever made it.

    Found WITHOUT the name derivation the delete above uses. That independence
    is the point: if this shared `prefixed_name` with the delete, a wrong
    derivation would blind the delete AND the check together -- both would ask
    about names nothing holds, find nothing, and agree. The first version of
    this fixture did exactly that, which is the defect being fixed here
    reappearing one level up.

    It reads the WHOLE broker on purpose and is not scoped to this run. The
    scoping belongs to the caller, which diffs two of these against each other;
    an earlier version did the scoping here, by prefix, and that was wrong --
    see the fixture below.
    """
    return {i.config.name for i in await all_streams(js) if "LIVE82" in i.config.name}


@pytest.fixture(autouse=True)
async def _cleanup_streams():
    """Remove this module's streams, addressed as the broker actually holds them.

    This deleted the BARE names, `LIVE82_EVENTS` and `LIVE82_DLQ`, in both
    directions. The service declares those specs through `ServiceConfig`, which
    renames them with the session and module prefix, so the broker held
    `t<session>_<module>_LIVE82_EVENTS` while the delete asked for a stream that
    never existed -- and `except Exception: pass` made removing nothing look
    exactly like removing them. 19 pairs survived on the shared broker that way,
    and only the session-wide sweep was collecting any of them.

    The assertion is the instrument, not the count: that sweep now removes these
    at session end regardless, so a broker census can no longer tell whether
    this fixture works. What can is asking, here, whether what it just deleted
    is gone.

    THE QUESTION IS "DID WHAT I CREATED SURVIVE", NOT "IS THE BROKER CLEAN".
    Those come apart, and the first version asked the second one: it scoped the
    leftover reading by session prefix, and `decided_prefix()` is None when
    isolation is off, so the filter collapsed to the bare marker and the
    assertion blamed this run for every LIVE82 stream on the broker -- naming
    other sessions' prefixes. That mode is the documented opt-out for
    "inspecting what a run left behind", i.e. precisely what someone is doing
    while chasing this leak, with leaked streams present.

    Taking a snapshot at setup and asserting on the DIFFERENCE answers the
    question this fixture is for and needs no notion of a prefix, so it behaves
    the same whether isolation is on or off.
    """
    nc = await nats.connect(broker_url())
    js = nc.jetstream()
    try:
        for name in await _this_modules_streams(js):
            await js.delete_stream(name)

        before = await _every_live82_stream(js)

        yield

        for name in await _this_modules_streams(js):
            try:
                await js.delete_stream(name)
            except Exception as exc:  # noqa: BLE001 - teardown must not fail a green run
                print(f"cleanup could not delete {name!r}: {exc}")

        appeared = sorted(await _every_live82_stream(js) - before)
        assert not appeared, f"this module left {len(appeared)} stream(s) behind: {appeared}"
    finally:
        await nc.close()


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_overlapping_listeners_burst():
    """Burst of 30 events published to overlapping subjects over live NATS.
    Verifies exact execution counts and zero duplicates under concurrent delivery.

    The counts are read after a sentinel published behind the burst has been handled and the
    handlers still running have finished: the sentinel reaches the service after every burst
    message, so a duplicate dispatch of one of them would already have been spawned. Stopping the
    wait at the target count would not see a duplicate arriving after it.
    """
    counts = {
        "wc": 0,
        "created": 0,
        "gt": 0,
    }
    lock = asyncio.Lock()
    sentinel_handled = asyncio.Event()

    class BurstService(CliffracerService):
        @listener("burst.orders.*", fanout=True)
        async def on_wc(self, subject: str, order_id: int = 0) -> None:
            async with lock:
                counts["wc"] += 1

        @listener("burst.orders.created", fanout=True)
        async def on_created(self, subject: str, order_id: int = 0) -> None:
            async with lock:
                counts["created"] += 1

        @listener("burst.sentinel", fanout=True)
        async def on_sentinel(self, subject: str) -> None:
            sentinel_handled.set()

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
                client_nc.publish(
                    HandlerDiscovery.with_namespace(svc.config, "burst.orders.created"),
                    json.dumps({"order_id": i}).encode(),
                )
            )
        for i in range(10):
            pub_tasks.append(
                client_nc.publish(
                    HandlerDiscovery.with_namespace(svc.config, "burst.orders.eu.created"),
                    json.dumps({"eu_id": i}).encode(),
                )
            )
        await asyncio.gather(*pub_tasks)
        await client_nc.flush()

        # The sentinel is published behind the burst on the same connection, so it is delivered
        # after every message in it; once it is handled, wait out the handlers still running.
        await client_nc.publish(
            HandlerDiscovery.with_namespace(svc.config, "burst.sentinel"), b"{}"
        )
        await client_nc.flush()
        await asyncio.wait_for(sentinel_handled.wait(), timeout=10.0)
        await svc.container.lifecycle.drain_active_tasks(timeout=10.0)

        async with lock:
            assert counts["wc"] == 20, f"Expected 20, got {counts['wc']}"
            assert counts["created"] == 20, f"Expected 20, got {counts['created']}"
            assert counts["gt"] == 30, f"Expected 30, got {counts['gt']}"

    finally:
        await client_nc.close()
        await svc.stop()


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
            StreamSpec(name="LIVE82_DLQ", subjects=["dlq.live82_pull_svc"]),
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

        await raw_nc.subscribe(HandlerDiscovery.dlq_subject(cfg), cb=on_dlq)

        # The server announces a terminated message, and only a terminated one: an acknowledged
        # message looks the same in `consumer_info` (nothing pending, nothing awaiting an ack).
        stream = prefixed_name("LIVE82_EVENTS")
        consumer = prefixed_name("live82-puller")
        terminations: list[dict] = []

        async def on_termination(msg):
            terminations.append(json.loads(msg.data.decode()))

        await raw_nc.subscribe(
            f"$JS.EVENT.ADVISORY.CONSUMER.MSG_TERMINATED.{stream}.{consumer}", cb=on_termination
        )
        await raw_nc.flush()

        # Publish malformed JSON to pull consumer stream
        await raw_js.publish(
            HandlerDiscovery.with_namespace(cfg, "live82.pull.events"),
            b"{{malformed-json-here",
            headers={"Content-Type": "application/json"},
        )

        await wait_until(
            lambda: dlq_messages, within=10.0, reason="the malformed message to be dead-lettered"
        )

        assert len(dlq_messages) == 1
        assert dlq_messages[0]["original_subject"] == HandlerDiscovery.with_namespace(
            cfg, "live82.pull.events"
        )
        assert "Decode error" in dlq_messages[0]["error"]

        # The message was terminated, not acknowledged and not left to be redelivered.
        await wait_until(
            lambda: terminations,
            within=10.0,
            reason="the server to announce the poison message as terminated",
        )
        assert len(terminations) == 1, terminations
        assert (terminations[0]["stream"], terminations[0]["consumer"]) == (stream, consumer)
        assert terminations[0]["deliveries"] == 1, terminations[0]

        # Verify consumer state: no pending, no ack_pending, no poison loop
        info = await raw_js.consumer_info(stream, consumer)
        assert info.num_pending == 0
        assert info.num_ack_pending == 0

    finally:
        await raw_nc.close()
        await svc.stop()


@pytest.mark.nats_required
@pytest.mark.asyncio
async def test_live_nats_auth_timer_concurrent_with_rpc():
    """Over live NATS: a timer runs as its bot user in the background, and the RPC answers the
    callers its role guard admits and refuses the rest.

    Both halves of `requires_roles` are read: authentication (no token, refused as
    unauthenticated) and authorization (a token without the role, refused as forbidden; a timer
    guarded by a role its bot lacks, which never runs).
    """
    timer_runs = 0
    forbidden_timer_runs = 0
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

        @timer(interval=0.05)
        @requires_roles("a_role_the_bot_lacks")
        async def forbidden_tick(self):
            nonlocal forbidden_timer_runs
            forbidden_timer_runs += 1

        @rpc
        @requires_roles("admin")
        async def secure_rpc(self) -> str:
            return "authenticated_rpc_success"

    admin = auth_svc.create_user(
        "live_admin", "admin@live.net", "an-admin-password", roles={"admin"}
    )
    viewer = auth_svc.create_user(
        "live_viewer", "viewer@live.net", "a-viewer-password", roles={"viewer"}
    )
    admin_token = auth_svc.authenticate(admin.username, "an-admin-password")
    viewer_token = auth_svc.authenticate(viewer.username, "a-viewer-password")
    assert admin_token and viewer_token

    svc = AuthTimerLiveService(ServiceConfig(name="live_auth_svc"))
    await svc.start()

    client_nc = await nats.connect(broker_url())
    rpc_subject = HandlerDiscovery.outbound_subject(
        svc.config, "live_auth_svc", "rpc", "secure_rpc"
    )

    async def call(token: str | None) -> dict:
        resp = await client_nc.request(
            rpc_subject,
            b"{}",
            timeout=5.0,
            headers={"Authorization": f"Bearer {token}"} if token else None,
        )
        return json.loads(resp.data.decode())

    try:
        # 1. Verify timer runs repeatedly and successfully under role guard
        for _ in range(30):
            if timer_runs >= 2:
                break
            await asyncio.sleep(0.05)
        assert timer_runs >= 2

        # The timer guarded by a role its bot user lacks was fired as often and never ran its body:
        # the role check is what stopped it, and the denial is the timer's recorded error.
        guarded = next(
            t for t in svc.container.registry.timers if t.method_name == "forbidden_tick"
        )
        assert guarded.error_count >= 1, "the timer the bot may not run was never fired"
        assert forbidden_timer_runs == 0
        assert "AuthorizationError" in (guarded.last_error or ""), guarded.last_error

        # 2. Unauthenticated RPC call over wire MUST BE REJECTED
        reply = await call(None)
        assert "refused: unauthenticated" in reply["error"], reply

        # 3. Authenticated, but without the role: refused as forbidden, not as unauthenticated
        reply = await call(viewer_token)
        assert reply["error"] == "refused: forbidden", reply

        # 4. Authenticated with the role: the handler runs
        reply = await call(admin_token)
        assert reply["result"] == "authenticated_rpc_success", reply

    finally:
        await client_nc.close()
        await svc.stop()
