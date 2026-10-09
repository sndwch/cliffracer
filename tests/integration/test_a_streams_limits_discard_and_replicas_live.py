"""A stream's declared limits, discard policy and replicas, against each broker image.

Each image runs in a disposable container on an ephemeral loopback port. What it reports back, what
an update does and what a single server does with a replica count were measured on these images;
the rows hold the behaviour that rests on it. A single server refuses more than one replica when a
stream is created, but accepts the change on an update and reports it back while keeping one copy,
so the copies are counted from the cluster information as well as the configuration.
"""

import asyncio
import subprocess
import uuid

import nats
import pytest
from nats.js.api import StreamConfig

from cliffracer.core.jetstream import (
    StreamDeclarationError,
    StreamSpec,
    ensure_streams,
    validate_bound_streams,
)
from tests.broker_isolation import prefixed_name, prefixed_subject

pytestmark = [pytest.mark.integration, pytest.mark.nats_required]


@pytest.fixture(params=["nats:2.10.29-alpine", "nats:2.11.2-alpine", "nats:2.12.0-alpine"])
async def js(request):
    name = "cliffracer-stream-limits-" + uuid.uuid4().hex
    subprocess.run(
        [
            "docker",
            "run",
            "-d",
            "--rm",
            "--name",
            name,
            "-p",
            "127.0.0.1::4222",
            request.param,
            "-js",
        ],
        check=True,
        capture_output=True,
        timeout=90,
    )
    nc = None
    try:
        address = subprocess.run(
            ["docker", "port", name, "4222/tcp"],
            check=True,
            capture_output=True,
            text=True,
            timeout=10,
        ).stdout.strip()
        assert address.startswith("127.0.0.1:"), address
        nc = nats.NATS()
        async with asyncio.timeout(10):
            await nc.connect(
                "nats://" + address,
                connect_timeout=0.2,
                max_reconnect_attempts=30,
                reconnect_time_wait=0.05,
            )
        yield nc.jetstream()
    finally:
        if nc is not None:
            await nc.close()
        subprocess.run(["docker", "rm", "-f", name], capture_output=True, timeout=30)


# Named as the rest of the tier names what it puts on a broker, though each image here is a broker
# of its own. Read when a test runs, since the run sets the prefix per test.
def stream() -> str:
    return prefixed_name("EVENTS")


def subjects() -> list[str]:
    return [prefixed_subject("events.>")]


def spec(**fields) -> StreamSpec:
    return StreamSpec(name=stream(), subjects=subjects(), **fields)


async def test_declared_limits_are_created_as_declared_and_a_second_boot_changes_nothing(js):
    declared = spec(max_msgs=10, max_bytes=100_000, discard="new", num_replicas=1)

    await ensure_streams(js, [declared])
    await ensure_streams(js, [declared])

    held = (await js.stream_info(stream())).config
    assert (
        held.max_msgs,
        held.max_bytes,
        StreamSpec._plain(held.discard, ""),
        held.num_replicas,
    ) == (
        10,
        100_000,
        "new",
        1,
    )


async def test_an_update_keeps_a_limit_the_operator_set_and_the_declaration_leaves_out(js):
    await js.add_stream(StreamConfig(name=stream(), subjects=subjects(), max_msgs=500))

    await ensure_streams(js, [spec(max_age_seconds=3600)], allow_update=True)

    held = (await js.stream_info(stream())).config
    assert (held.max_msgs, held.max_age) == (500, 3600)


async def test_a_limit_below_what_the_stream_holds_is_refused_and_one_at_it_applies(js):
    await ensure_streams(js, [spec(max_msgs=100)])
    for _ in range(10):
        await js.publish(prefixed_subject("events.x"), b"x" * 100)

    with pytest.raises(StreamDeclarationError, match="max_msgs declared 5, the stream holds 10"):
        await ensure_streams(js, [spec(max_msgs=5)], allow_update=True)
    assert (await js.stream_info(stream())).state.messages == 10

    await ensure_streams(js, [spec(max_msgs=10)], allow_update=True)
    info = await js.stream_info(stream())
    assert (info.config.max_msgs, info.state.messages) == (10, 10)


async def test_a_single_server_refuses_more_replicas_when_a_stream_is_created(js):
    with pytest.raises(Exception, match="replicas > 1 not supported in non-clustered mode"):
        await ensure_streams(js, [spec(num_replicas=3)])


async def test_a_replica_count_a_single_server_accepted_on_update_is_refused_by_bind(js):
    await js.add_stream(StreamConfig(name=stream(), subjects=subjects()))
    current = (await js.stream_info(stream())).config
    await js.update_stream(current.evolve(num_replicas=3))
    assert (await js.stream_info(stream())).config.num_replicas == 3, "the update was not accepted"

    with pytest.raises(StreamDeclarationError, match="configured 3, 1 live, not clustered"):
        await validate_bound_streams(js, [spec(num_replicas=3)])


async def test_provision_refuses_a_replica_count_the_update_did_not_give_the_stream(js):
    await ensure_streams(js, [spec()])

    with pytest.raises(StreamDeclarationError, match="operator action") as refused:
        await ensure_streams(js, [spec(num_replicas=3)], allow_update=True)
    assert "configured 3, 1 live, not clustered" in str(refused.value)


async def test_bind_names_a_declared_limit_the_broker_does_not_hold(js):
    await js.add_stream(StreamConfig(name=stream(), subjects=subjects()))

    with pytest.raises(StreamDeclarationError, match="max_msgs is 'no limit', expected 100"):
        await validate_bound_streams(js, [spec(max_msgs=100)])
