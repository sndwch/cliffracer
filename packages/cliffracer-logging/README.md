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

## `LoggingExtension(to_nats=False, timing=True, log_level="INFO", redactor=...)`

| | |
|---|---|
| `to_nats` | stream this service's logs to `logs.<service>.<level>`: the records bound to this service (`extra["service"]` is its name), which are the ones the framework and `get_service_logger` write. A record with no `service` binding, such as a host application's own `logger.info`, is not streamed, in a process that configured one service or several; bind one with `logger.bind(service=...)`, or add a sink of your own |
| `timing` | log `"<kind> <subject> <ms>ms <ok or failed=ErrorName>"` at DEBUG for every dispatch; a timer has no subject, so its method name stands in |
| `log_level` | the lowest level the NATS sink streams, `"INFO"` by default; the `timing` lines are written at DEBUG, so they reach the stream only when this is `"DEBUG"` |
| `redactor` | transform each structured record before NATS publication; credential fields are redacted by default (see NATS log privacy) |

`health_details()` reports `{"to_nats": bool, "streaming": bool}`. `streaming`
is whether a sink is attached, so a service configured for NATS while its
connection is down reports `to_nats: true, streaming: false`.

Once the sink has been started, `health_details()` also carries `nats_sink`, what
the sink did with the records it was handed: `published` (accepted by the NATS
client), `failed` (could not be built or were refused), `dropped` (not scheduled
because the backlog was full or the event loop was gone), `pending` (in flight) and
`last_error` (the latest loss's exception type). `streaming` says the sink is
attached; these say whether anything is getting through it.

The sink lets at most `max_pending` publishes be in flight, 1000 by default, and
drops and counts a record that arrives when that many are out, because a log call
does not wait on the broker. `LoggingConfig.add_nats_sink` takes `max_pending` and a
`NatsSinkStats` to count into. Losses go to stderr when the error differs from the
last one reported, and are otherwise only counted.

`stop()` removes the sink it added, using the handler id loguru returned when it
was attached. A service that stops and starts again therefore has one sink.

## What timing covers

`timing` measures whatever runs through the hook chain. Five kinds reach it
today, and each appears as the first word of the log line: `rpc`, `async_rpc`,
`describe` (a `{service}.describe` request), `event` and `timer`.

The container runs the chain for every dispatch, so a new dispatch path is
timed without a change here. `tests/repo/test_instrumentation_coverage.py`
checks that each callback the container hands the broker client reaches that
chain.

Outbound calls are logged by core itself: `call_rpc`, `call_async` and
`broadcast_message` each write their own line. `connect` and `disconnect` are
visible through the container's NATS callbacks.

## `LoggingConfig.configure(service_name, ...)`

Structured JSON or human-readable logging to console and rotating files. It is a
`@staticmethod`, so call it on the class.

`service_name` is the first positional argument and is required. The level
keyword is `log_level`, and it defaults to `"INFO"`.

It returns the ids of the sinks it added, so a caller can remove just those with
`logger.remove(id)`. `replace_existing=True`, the default, removes every sink loguru
held before the call, the host's own included, once this service's sinks are
installed; `replace_existing=False` adds this service's sinks next to what is already
installed.

A sink that cannot be opened (a log directory that exists but cannot be written, a
`rotation`, `retention` or `compression` loguru refuses) raises what loguru raises, and
the process's logging is as it was: the sinks that were in place are still in place and
none of this call's are left behind.

`service` is merged into the process's global loguru `extra`, so context the host
set (app, region, version) stays on every record. Loguru's `extra` is process-wide, so it holds one
name: with the default `replace_existing=True` it is this service's, and a line written through the
plain `logger` carries it in a text or JSON sink. With `replace_existing=False` the sinks of an
earlier service stay installed, so a `service` an earlier `configure` or setup already named is kept
and one WARNING names both services; the first `configure` names it. A `ContextualLogger` from
`get_service_logger` labels its own lines with its own name.

That process-wide `service` is a stamp, not a binding. It is an instance of a private `str` subclass,
which formats and serialises as the name it holds and is kept by loguru through filters and through
`enqueue=True` sinks, and the NATS sink streams a record only when its `service` is the sink's name
and a call bound it. A line nothing bound a service to is therefore not streamed, whichever service
was configured. Two limits: a host that stores its own `service` with
`logger.configure(extra={"service": ...})` has stored a plain string, which is read as a binding;
and a call that binds the stamp object itself (`logger.bind(service=extra["service"])`) passes the
marker along, so it is read as the stamp (bind `str(extra["service"])`, or the name, instead).

## Correlation-filtered logging

`setup_correlation_logging` and `get_correlation_logger` attach the current
correlation id to every record.

`setup_correlation_logging` removes every sink loguru held before the call, the host's
own included, as `LoggingConfig.configure` does by default. Pass
`replace_existing=False` to add its sinks next to what is already installed; a sink
installed before the call is registered before the correlation sinks, so it does not see
the `correlation_id` and `service` keys they fill.

`setup_correlation_logging` with the default `replace_existing=True` also makes its service the
process-wide `service`, merged into the global `extra` as `LoggingConfig.configure` does, so a later
setup names the service it was given on every line after it. With `replace_existing=False` the
process-wide `service` is left as it is: when an earlier setup named another service, lines written
through the plain `logger` keep that name and one WARNING names both. A service a call binds
(`logger.bind(service=...)`) wins over the process-wide one in both cases.

Installed from PyPI, versioned in lockstep with `cliffracer`.

## NATS log privacy

NATS log streaming publishes the structured record, including bound `extra`
fields. The default redactor recursively replaces values under credential
keys: any key that contains `password`, `authorization`, `api_key`, `secret`,
`token`, `jwt`, `bearer`, `session`, `passphrase`, `signing_key` or the like,
is `cookie` or `set-cookie`, or is the header an installed extension reads a
credential from (`AuthExtension(header=...)`). It is the rule the dead-letter
publisher applies to message headers
(`cliffracer.core.credentials.is_credential_name`), in any case and with `-`,
`_` and `.` read as the same. A service can supply a stricter domain policy:

```python
def redact_customer_data(record):
    record["record"]["extra"].pop("customer_email", None)
    return record

logging = LoggingExtension(to_nats=True, redactor=redact_customer_data)
```

The redactor runs before publication. If it raises or returns a record that
cannot be serialized, that record is not sent. Free-form message text cannot
be classified by field name, so applications must not interpolate credentials
into log messages.
