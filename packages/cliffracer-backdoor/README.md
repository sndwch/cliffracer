# cliffracer-backdoor

A live debug console inside a running cliffracer service, and the client that
connects to it.

```python
from cliffracer import CliffracerService
from cliffracer_backdoor import BackdoorExtension

class Orders(CliffracerService):
    backdoor = BackdoorExtension(enabled=True, port=0)
```

Then, from a shell on the same host:

```
cliffracer-backdoor 127.0.0.1:<port>
```

`port=0` asks the OS for a free port. The bound port is written to the logs and
reported in `/health` under this extension's name.

Settings also come from the environment, with the prefix `CLIFFRACER_BACKDOOR_`:
`CLIFFRACER_BACKDOOR_ENABLED`, `_PORT`, `_PASSWORD`.

## Turning it on is a deliberate act

`enabled` defaults to `False`. The console evaluates arbitrary Python inside the
service process, which makes it a remote code execution endpoint. Turn it on
where you want that and leave it off elsewhere.

It binds the literal address `127.0.0.1`. A name that resolves to both loopback
families would make `start_server` bind two sockets, and under `port=0` each
would get a different ephemeral port, so the port reported in `/health` would
answer on only one of them.

## The client reports failure

`cliffracer-backdoor host:port` opens a socket to the endpoint before
connecting. When nothing is listening it prints one line to stderr and exits
non-zero.

The probe is what gives the command its exit status. `BackdoorClient.connect`
shells out to `nc` and then `telnet`, and when both are missing it prints
instructions and returns normally.

## Inside the console

`service` is the live instance. `inspect_service()`, `inspect_nats()` and
`show_handlers()` summarise it. `await service.some_rpc()` calls a handler
directly. `help_backdoor()` lists the rest.

## Extending it

`ServiceInspector` and `NATSInspector` build the console's namespace. Subclass
either, or pass your own callables, to show an operator whatever your service
should show.

Installed from PyPI, versioned in lockstep with `cliffracer`.
