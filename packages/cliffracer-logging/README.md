# cliffracer-logging

NATS log streaming, correlation-filtered sinks, structured logging setup and
per-dispatch timing logs for [cliffracer](../../README.md) services.

Core uses `loguru` for its own log lines. This package is what configures
logging: sinks, formats and files.

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer_logging import LoggingExtension

class Orders(CliffracerService):
    logging = LoggingExtension(to_nats=True)

    @rpc
    async def place(self, item: str) -> dict[str, str]:
        return {"ok": item}
```

## `LoggingExtension(to_nats=False, timing=True)`

| | |
|---|---|
| `to_nats` | stream this service's logs to `logs.<service>.<level>` |
| `timing` | log `"<kind> <subject> <ms>ms"` at DEBUG for every dispatch |

`health_details()` reports `{"to_nats": bool, "streaming": bool}`. `streaming`
is whether a sink is attached, so a service configured for NATS while its
connection is down reports `to_nats: true, streaming: false`.

`stop()` removes the sink it added, using the handler id loguru returned when it
was attached. A service that stops and starts again therefore has one sink.

## What timing covers

`timing` measures whatever runs through the hook chain. Four kinds reach it
today, and each appears as the first word of the log line: `rpc`, `async_rpc`,
`event` and `timer`.

The container runs the chain for every dispatch, so new dispatch paths are 
timed automatically. This is verified by `tests/unit/test_instrumentation_coverage.py`.

Outbound calls are logged by core itself: `call_rpc`, `call_async` and
`broadcast_message` each write their own line. `connect` and `disconnect` are
visible through the container's NATS callbacks.

## `LoggingConfig.configure(service_name, ...)`

Structured JSON or human-readable logging to console and rotating files. It is a
`@staticmethod`, so call it on the class.

`service_name` is the first positional argument and is required. The level
keyword is `log_level`, and it defaults to `"INFO"`.

## Correlation-filtered logging

`setup_correlation_logging`, `get_correlation_logger` and
`CorrelationLoggerMixin` attach the current correlation id to every record.

Installed from PyPI, versioned in lockstep with `cliffracer`.
