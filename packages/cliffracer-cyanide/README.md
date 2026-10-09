# cliffracer-cyanide

Fault injection and mundane failure modes for Cliffracer services.

## Overview

Chaos engineering and reliability testing require verifiable failure modes within service
components:
- **Delay Injection**: Delays request execution to test timeouts and concurrent capacity.
- **Fault Raising**: Raises typed exceptions after a delay to test error handling and recovery.
- **Timeout Simulation**: Sleeps past caller timeout deadlines to test cancellation handling
  and concurrency permit restoration without resource leakage.
- **Reply Dropping**: Suppresses the wire response to simulate message loss or network partitions.

## Usage

Declare `CyanideExtension` as a class attribute on your service:

```python
from cliffracer import CliffracerService, rpc
from cliffracer_cyanide import CyanideConfig, CyanideExtension

class PaymentService(CliffracerService):
    cyanide = CyanideExtension(config=CyanideConfig(enabled=True))

    @rpc
    async def process_payment(self, amount: float) -> dict[str, str]:
        await self.cyanide.slow()
        return {"status": "success"}
```

## Failure Modes

The extension provides four mundane failure modes:
- `slow(delay=None)`: Delays response by a configured duration using asynchronous sleep.
- `raise_after_delay(delay=None, message="Injected cyanide failure")`: Delays execution and raises `CyanideFaultError`.
- `sleep_past_timeout(duration=None)`: Sleeps past standard caller timeout deadlines to verify cancellation and permit reclamation. The default duration is 60 seconds, past the 30 seconds that `ServiceClient` and `ServiceConfig.request_timeout` default to; a caller or service with a longer timeout needs a longer `sleep_timeout_duration`.
- `drop_reply(ctx)`: Sets `ctx.raw.respond` to an asynchronous no-op coroutine to drop wire responses.

## Choosing a mode

With `enabled=True`, a message gets a mode from the first of these that names one:

1. a request header: `x-cyanide-mode` (or `x-cyanide-fault`);
2. a mode set for its handler with `configure_handler(handler_name, mode)`;
3. the service-wide mode, from `CyanideConfig(mode=...)` or `set_mode(mode)`.

A mode is spelled `slow`, `raise_after_delay`, `sleep_past_timeout`, `drop_reply` or `random`, case-insensitive with `-` read as `_`. Each failure mode also answers to short names: `raise_delay`, `raise` and `fault` for `raise_after_delay`; `timeout` and `sleep_timeout` for `sleep_past_timeout`; `drop` for `drop_reply`. `CyanideConfig`, `set_mode` and `configure_handler` raise `ValueError` for any other name. A header that names no mode is logged and ignored: it is the caller's text, and the service does not refuse the request for it.

These headers set the parameters of the mode a message got:

| Header | Applies to | Meaning |
| --- | --- | --- |
| `x-cyanide-delay` | `slow`, `raise_after_delay` | delay in seconds |
| `x-cyanide-duration` | `sleep_past_timeout` | sleep in seconds |
| `x-cyanide-message` | `raise_after_delay` | text of the `CyanideFaultError` |

### Random mode

`mode="random"` draws a failure mode per message, from `seed` and the message, so a soak injects a mix. The four weights are the share of messages that get each mode: `slow_weight`, `drop_reply_weight`, `raise_after_delay_weight` and `sleep_past_timeout_weight`. Each is between 0 and 1 and together they add up to at most 1; the rest of the messages are left alone. `CyanideConfig` refuses weights that do not fit. `seed` is any string or integer; unset, the extension makes one at start and logs it at WARNING, and `/health` and `/info` report it.

A run with the same seed injects the same faults. What identifies a message is the correlation id its caller sent (in a header such as `x-correlation-id`, or as `correlation_id` in the payload): the draw for an id is the same whatever order the messages arrive in. A message that carries no id of its own is identified by its subject, its payload, and how many identical ones came before it, so a load generator that sends one payload thousands of times gets the configured mix, and the same mix on a replay. The extension keeps that count for the last `injection_record_limit` distinct subject and payload pairs; a pair it has forgotten starts again at its first draw, so a replay is exact while a run has no more distinct messages than that.

The replay is exact for traffic whose senders send no id of their own (a plain NATS publisher or requester) and for traffic that reuses ids. It is not exact for traffic sent through cliffracer's own senders (`ServiceClient`, and a service's `call_rpc`, `call_async` and `publish_event`): they always send a correlation id, and make a new one for each request when none is set, so under this rule each request is identified by an id that differs from run to run. To replay such a run, give each request an id that is the same in every run (set the `X-Correlation-ID` header of the `ServiceClient`, or the correlation id in the sending service's context, from the request's number).

### The record of what was injected

Every injection is remembered, so a run can tell a message cyanide faulted from one that was lost.

- `injections()` returns the remembered `Injection` entries (`correlation_id`, `subject`, `mode`, `seed`), oldest first.
- `drain_injections()` returns them and forgets them.
- `injections_dropped` counts the entries evicted once the record held `injection_record_limit` (default 1024). When it is not 0, a message with no entry may still have been faulted.

`/health` reports `injections_recorded`, `injections_dropped` and `injection_record_limit`.

## Configuration

All failure modes are disabled by default. Configuration is managed through `CyanideConfig`
or environment variables prefixed with `CLIFFRACER_CYANIDE_`:

- `CLIFFRACER_CYANIDE_ENABLED`: Set to true to activate failure injection.
- `CLIFFRACER_CYANIDE_SLOW_DELAY`: Delay in seconds for slow mode (default: 1.0).
- `CLIFFRACER_CYANIDE_RAISE_DELAY`: Delay in seconds before raising fault (default: 0.5).
- `CLIFFRACER_CYANIDE_SLEEP_TIMEOUT_DURATION`: Sleep duration in seconds for timeout simulation (default: 60.0).
- `CLIFFRACER_CYANIDE_MODE`: Default failure mode to execute in request interceptor.
- `CLIFFRACER_CYANIDE_SLOW_WEIGHT`, `CLIFFRACER_CYANIDE_DROP_REPLY_WEIGHT`, `CLIFFRACER_CYANIDE_RAISE_AFTER_DELAY_WEIGHT`, `CLIFFRACER_CYANIDE_SLEEP_PAST_TIMEOUT_WEIGHT`: The shares for random mode (default: 0.0 each; each in [0, 1], total at most 1).
- `CLIFFRACER_CYANIDE_SEED`: The seed for random mode (default: a fresh one per start).
- `CLIFFRACER_CYANIDE_INJECTION_RECORD_LIMIT`: How many injections the record keeps (default: 1024, at least 1).

When disabled, any failure mode requested via headers is safely ignored, and direct code invocation raises `CyanideDisabledError`.
