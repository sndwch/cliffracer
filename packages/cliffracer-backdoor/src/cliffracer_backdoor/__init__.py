"""Live debugging console for cliffracer services.

`BackdoorExtension` is the part that wires itself: declare it on a service and
the container starts a console on the configured port when it is enabled.

`ServiceInspector` and `NATSInspector` are LIBRARY CLASSES -- the backdoor
console builds them for the interactive namespace, and a consumer can construct
them directly. `BackdoorClient` is what `cliffracer-backdoor` uses to connect.

ONE SWITCH: BackdoorConfig.enabled, settable in the constructor or as
CLIFFRACER_BACKDOOR_ENABLED, defaulting False. A debug console is a remote code
execution endpoint, so the default is off and a single named switch is what
turns it on -- a second spelling of "off" is a second thing to get wrong.
"""

from cliffracer_backdoor.backdoor import BackdoorClient, BackdoorServer
from cliffracer_backdoor.extension import BackdoorConfig, BackdoorExtension
from cliffracer_backdoor.inspector import NATSInspector, ServiceInspector

__all__ = [
    "BackdoorClient",
    "BackdoorConfig",
    "BackdoorExtension",
    "BackdoorServer",
    "NATSInspector",
    "ServiceInspector",
]
