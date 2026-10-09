"""`describe(...).queue_group` is the `queue=` the runtime passes when it subscribes.

Describe computes the queue group itself, as a second copy of the rule in
`Container._setup_subscriptions`, and no test compared the two. They disagreed under a subject
prefix: the runtime queues on the prefixed durable (`prod_push_worker`) and describe said the
declared name. This runs the real subscription setup against mocked connections and reads, for
every listener, the queue it was given.
"""

from unittest.mock import AsyncMock

import pytest
from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, listener, validated_listener
from cliffracer.core.jetstream import StreamSpec
from cliffracer.introspect import describe

pytestmark = pytest.mark.unit


class Evt(BaseModel):
    n: int


class Mixed(CliffracerService):
    @listener("tasks.push", durable="push_worker")
    async def on_push(self, n: int) -> None: ...

    @listener("tasks.pull", durable="pull_worker", pull=True)
    async def on_pull(self, n: int) -> None: ...

    @listener("alerts.all", fanout=True)
    async def on_alert(self, n: int) -> None: ...

    @validated_listener("events.v", Evt, durable="val_worker")
    async def on_event(self, event: Evt) -> None: ...


def _config(prefix: str | None, *, jetstream: bool, namespace: str | None = None) -> ServiceConfig:
    stem = f"{prefix}." if prefix else ""
    return ServiceConfig(
        name="s",
        health_port=0,
        namespace=namespace,
        subject_prefix=prefix,
        jetstream_enabled=jetstream,
        jetstream_streams=[
            StreamSpec(
                name="ALL", subjects=[f"{stem}tasks.>", f"{stem}alerts.>", f"{stem}events.>"]
            )
        ],
    )


async def _runtime_queues(
    cls: type[CliffracerService], config: ServiceConfig
) -> dict[str, str | None]:
    """The queue each event subject was subscribed with, by subject, None when it had none."""
    service = cls(config)
    service.nc, service.js = AsyncMock(), AsyncMock()
    service._discover_handlers()
    await service.container._setup_subscriptions()
    events = set(service.container.registry.event_handlers)
    queues: dict[str, str | None] = {}
    for call in service.js.subscribe.call_args_list:
        queues[call.args[0]] = call.kwargs.get("queue")
    for call in service.js.pull_subscribe.call_args_list:
        queues[call.args[0]] = call.kwargs.get("queue")
    for call in service.nc.subscribe.call_args_list:
        if call.args[0] in events:
            queues[call.args[0]] = call.kwargs.get("queue")
    return queues


@pytest.mark.asyncio
@pytest.mark.parametrize("prefix", [None, "prod"])
async def test_every_listeners_queue_group_is_the_queue_the_runtime_subscribed_with(prefix):
    config = _config(prefix, jetstream=True)

    runtime = await _runtime_queues(Mixed, config)
    described = describe(Mixed, config=config)

    assert len(described.listeners) == 4 == len(runtime)
    for item in described.listeners:
        assert item.effective_subject in runtime, (item.effective_subject, sorted(runtime))
        assert item.queue_group == runtime[item.effective_subject], item.effective_subject
    assert {item.queue_group for item in described.listeners} == {
        None,
        config.prefixed_name("push_worker"),
        config.prefixed_name("val_worker"),
    }


class Broadcast(CliffracerService):
    @listener("alerts.all", fanout=True)
    async def on_alert(self, n: int) -> None: ...


@pytest.mark.asyncio
async def test_with_jetstream_off_nothing_has_a_queue_group_and_the_runtime_agrees():
    config = _config(None, jetstream=False)

    runtime = await _runtime_queues(Broadcast, config)
    described = describe(Broadcast, config=config)

    assert [item.queue_group for item in described.listeners] == [None]
    assert runtime == {"alerts.all": None}


class FanoutWithADurable(CliffracerService):
    @listener("alerts.all", durable="ignored", fanout=True)
    async def on_alert(self, n: int) -> None: ...


@pytest.mark.asyncio
async def test_a_fanout_listener_that_names_a_durable_is_subscribed_and_described_with_no_queue():
    config = _config("prod", jetstream=False)

    runtime = await _runtime_queues(FanoutWithADurable, config)
    described = describe(FanoutWithADurable, config=config)

    assert [item.queue_group for item in described.listeners] == [None]
    assert runtime == {"prod.alerts.all": None}
    assert [item.queue_group for item in describe(FanoutWithADurable).listeners] == [None]


def test_with_no_config_the_queue_group_is_the_declared_durable():
    described = describe(Mixed)

    assert {item.pattern: item.queue_group for item in described.listeners} == {
        "tasks.push": "push_worker",
        "tasks.pull": None,
        "alerts.all": None,
        "events.v": "val_worker",
    }
