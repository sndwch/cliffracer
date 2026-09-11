# Debug console

`BackdoorExtension` runs a Python console inside a running service. You connect
to it over a loopback socket, authenticate with a password, and get a prompt
with the service instance bound to a name. `await` works at that prompt, so you
can call the service's own coroutines.

The console evaluates arbitrary Python in the service process. It stays off
until you enable it.

## Enable it

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_backdoor import BackdoorExtension


class Orders(CliffracerService):
    backdoor = BackdoorExtension(enabled=True)


service = Orders(ServiceConfig(name="orders"))
await service.start()
```

`start()` is what brings extensions up. Connecting the broker alone leaves the
console unstarted.

## Settings

`BackdoorConfig` reads the environment under the prefix `CLIFFRACER_BACKDOOR_`.
A constructor argument beats the environment.

| setting | default | environment variable | meaning |
|---|---|---|---|
| `enabled` | `False` | `CLIFFRACER_BACKDOOR_ENABLED` | start the console |
| `port` | `0` | `CLIFFRACER_BACKDOOR_PORT` | `0` asks the OS for a free port |
| `password` | `None` | `CLIFFRACER_BACKDOOR_PASSWORD` | required to log in |

Enabling the console without a password generates a random one, logs it at
WARNING, and prints it at INFO. Set a password of your own when the log is
somewhere other people can read.

## Connect

The console listens on `127.0.0.1` and reports the port it bound on `/health`,
under the name you gave the extension:

```json
{"backdoor": {"enabled": true, "port": 43117}}
```

That is the port to give the client:

```bash
cliffracer-backdoor 127.0.0.1:43117
```

With `port=0` the number changes each run, so read it from `/health` or from
the service log rather than pinning it. Set `port` to fix it.

## At the prompt

Eight names are bound:

| name | what it is |
|---|---|
| `service` | the running service instance |
| `nc` | its NATS connection |
| `js` | its JetStream context |
| `asyncio` | the module |
| `help_backdoor()` | lists these commands |
| `inspect_service()` | service class, name, NATS URL with credentials redacted, request timeout |
| `inspect_nats()` | connection state, server, subscription count |
| `show_handlers()` | registered RPC and event handlers |

```pycon
>>> inspect_service()
>>> await service.process_order({"customer_id": "123"})
```

## Authentication

Each connection is asked for the password and has 30 seconds to answer. Three
failures lock that IP address out for five minutes.

## Running it safely

- The listener binds the literal address `127.0.0.1`, so reaching it means
  reaching the host first.
- `enabled` defaults to `False`, so a service gets a console only where someone
  asked for one.
- Set `password` explicitly for anything long-lived, and keep the port off
  published or forwarded interfaces.
- Turn it off by leaving `enabled` at its default, or by setting
  `CLIFFRACER_BACKDOOR_ENABLED=false`.

## Requirements

The prompt is `aioconsole`, which `cliffracer-backdoor` depends on, so
installing the distribution is all it takes.

## Inspecting from your own code

`ServiceInspector` and `NATSInspector` are library classes. The console builds
them for its own commands, and you can construct them directly:

```python
from cliffracer_backdoor import NATSInspector, ServiceInspector

info = ServiceInspector(service).get_info()
```
