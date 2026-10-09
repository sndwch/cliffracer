"""Lets a test service replace one of the container's lifecycle phases with a method of its own.

The container owns the phases a service goes through to start and stop: setting up and starting
its extensions, subscribing, stopping timers, stopping extensions. Several tests stand in for one
of them (to count it, to make it fail, to skip real subscriptions), and used to do it by defining
a method of the same name on the service subclass, which the container found by name. It no
longer looks there, because a production service that happened to define such a method turned the
phase off without a word.

This mixin keeps the test's way of saying it and puts the replacement where it belongs. After the
service is built, each phase method the test class defines is made the lifecycle's hook for that
phase, looked up when the phase runs. The container's own method is left alone, so a replacement
that forwards to `self.container._stop_timers()` still reaches it. Production code does not use
this, and a class that defines none of the names is unchanged.
"""

from __future__ import annotations

from typing import Any

# The service method a test defines, and the lifecycle hook it takes the place of.
PHASES = {
    "_setup_extensions": "setup_extensions",
    "_start_extensions": "start_extensions",
    "_setup_subscriptions": "setup_subscriptions",
    "_stop_timers": "stop_timers",
    "_stop_extensions": "stop_extensions",
}


class ServicePhases:
    """Mix in before `CliffracerService`: `class Svc(ServicePhases, CliffracerService)`."""

    def __init__(self, *args: Any, **kwargs: Any) -> None:
        super().__init__(*args, **kwargs)
        hooks = self.container.lifecycle.hooks  # type: ignore[attr-defined]
        for method_name, hook in PHASES.items():
            if method_name in dir(type(self)):
                setattr(hooks, hook, lambda name=method_name: getattr(self, name)())
