"""Each event listener's subscription, held so that one listener can be stopped and started alone.

The container subscribes every listener at startup through `subscribe`, which keeps the task that
holds the subscription by subject. A listener declared with `pause_when_down` is stopped by
cancelling that task, which drops the replica's interest in its durable, and started again by
subscribing it afresh; `ListenerPauses` decides when.
"""

from __future__ import annotations

import asyncio
from functools import partial
from typing import TYPE_CHECKING, Any

from .connection import flush_through_buffered_commands
from .jetstream import consumer_config_for
from .listener_pause import ListenerPauses

if TYPE_CHECKING:
    from .container import Container


class ListenerSubscriptions:
    """The subscription task of each event listener of one container, by subject."""

    def __init__(self, container: Container) -> None:
        self._c = container
        #: The task holding each listener's subscription: what a pause cancels.
        self.tasks: dict[str, asyncio.Task[Any]] = {}

    async def subscribe(self, pattern: str) -> None:
        """Subscribe one event listener as its declaration asks, and hold its task by subject."""
        assert self._c.connection.nc is not None
        # The callbacks bind straight to the dispatcher, as the container's own do.
        dispatcher = self._c.dispatcher
        sub: Any
        # Read once and prefixed here, so every use below -- the pull
        # consumer, the queue group and the push consumer -- carries it. A
        # durable is global to its stream, so two environments sharing a
        # broker would otherwise share one consumer and each take half the
        # messages.
        declared_durable = self._c.registry.event_durables.get(pattern)
        durable = self._c.config.prefixed_name(declared_durable) if declared_durable else None
        if (
            self._c.connection.jetstream_active
            and durable
            and pattern in self._c.registry.event_pull
        ):
            assert self._c.connection.js is not None
            binding = await self._c._bound_consumer_for(pattern, durable, pull=True)
            if binding is not None:
                stream, _ = binding
                pull_sub = await self._c.connection.js.pull_subscribe_bind(
                    consumer=durable,
                    stream=stream,
                )
            else:
                pull_sub = await self._c.connection.js.pull_subscribe(
                    pattern,
                    durable=durable,
                    config=consumer_config_for(self._c.config),
                )
            self._c.connection.track_subscription(pull_sub)
            await self._c.dispatcher.report_consumer_drift(pull_sub, durable, pattern=pattern)
            self._hold(
                pattern,
                asyncio.create_task(
                    self._c.dispatcher.pull_loop(
                        pull_sub,
                        durable,
                        pattern=pattern,
                        is_running_fn=lambda: self._c.is_running,
                        unsubscribe=partial(self._c.connection.unsubscribe, pull_sub),
                    )
                ),
            )
            return
        if self._c.connection.jetstream_active and durable:
            assert self._c.connection.js is not None
            binding = await self._c._bound_consumer_for(pattern, durable, pull=False)
            if binding is not None:
                stream, info = binding
                sub = await self._c.connection.js.subscribe_bind(
                    stream=stream,
                    config=info.config,
                    consumer=durable,
                    cb=dispatcher.make_jetstream_event_callback(pattern),
                    manual_ack=True,
                )
            else:
                sub = await self._c.connection.js.subscribe(
                    pattern,
                    queue=durable,
                    cb=dispatcher.make_jetstream_event_callback(pattern),
                    durable=durable,
                    manual_ack=True,
                    config=consumer_config_for(self._c.config),
                )
            self._c.connection.track_subscription(sub)
            await self._c.dispatcher.report_consumer_drift(sub, durable, pattern=pattern)
        else:
            sub = await self._c.connection.nc.subscribe(
                pattern, cb=dispatcher.make_event_callback(pattern)
            )
            self._c.connection.track_subscription(sub)
        self._hold(pattern, asyncio.create_task(self._c._subscription_handler(sub)))

    def _hold(self, pattern: str, task: asyncio.Task[Any]) -> None:
        self._c.connection.subscriptions.add(task)
        self.tasks[pattern] = task

    def start_pauses(self) -> ListenerPauses:
        """Start the background probe for the listeners declared with `pause_when_down`."""
        pauses = ListenerPauses(
            listeners=dict(self._c.registry.event_pause_when_down),
            dependencies=lambda: list(self._c.registry.dependencies),
            config=self._c.config,
            pause=self.pause,
            resume=self.resume,
            logger=self._c.logger,
            notify=self._c.extension_pipeline.run_listener_hook,
            streams=list(self._c.config.effective_jetstream_streams),
        )
        self._c.connection.subscriptions.add(
            asyncio.create_task(pauses.run(lambda: self._c.is_running), name="listener_pauses")
        )
        return pauses

    async def pause(self, subject: str) -> None:
        """Stop this replica consuming one listener: drop its interest, keep its durable.

        The task holding the subscription is cancelled, which unsubscribes a push durable or ends
        a pull loop. The durable stays on the server with its messages, and nothing is delivered
        to a durable nobody is bound to, so no delivery attempt is spent while paused. Messages
        already being handled finish as they would have.
        """
        task = self.tasks.pop(subject, None)
        if task is None:
            return
        self._c.connection.subscriptions.discard(task)
        task.cancel()
        await asyncio.gather(task, return_exceptions=True)
        if self._c.connection.nc is not None and self._c.connection.is_connected:
            await flush_through_buffered_commands(self._c.connection.nc)

    async def resume(self, subject: str) -> None:
        """Bind a paused listener's durable again, as its declaration asks."""
        if subject in self.tasks or not self._c.is_running:
            return
        await self.subscribe(subject)
        await flush_through_buffered_commands(self._c.connection.nc)
