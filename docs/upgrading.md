# Upgrading from 1.0.0

The changes a deployment moving from 1.0.0 to the release this page ships with has to act on. Each entry says what you see when the old code meets
the current release, what to change, and shows the code before and after. Where an entry leaves a
choice to the operator, it says so and names the document that holds the ruling; this page decides
nothing.

The entries are the ones the changelog marks **Breaking** or **Removed**. The changelog states
every change in one bullet; this page is the longer form for the ones that stop a service starting,
change what a call returns or raises, change something on the broker, or take away something a
deployment could have been using. The entries it marks **Behaviour change** and **API Change** are
in [CHANGELOG.md](../CHANGELOG.md).

## Startup and configuration

A service that fails here fails before it connects, so a rollout stops at the first replica and no
traffic reaches a broken one. The rule behind every refusal in this part is
[ADR-0010](decisions.md#adr-0010-fail-fast-startup-validation-over-permissive-defaults).

### A config names one way to authenticate to NATS

<!-- changelog.d: a-config-naming-two-nats-credentials-is-refused.md -->

**You see.** `ServiceConfig(...)` raises a `ValidationError` when it is built, if more than one of
`nats_user` with `nats_password`, `nats_token` and `nats_credentials_file` is set, or if
`nats_user` and `nats_password` are not set together.

**Change.** Set exactly one method.

**Your choice.** Which one. The refusal exists because two methods together were resolved by
nats-py's own precedence, so the configuration did not say which credential the broker saw.

```text
from cliffracer import ServiceConfig

config = ServiceConfig(name="orders", nats_user="orders", nats_password="s3cret", nats_token="t0ken")
```

```python
from cliffracer import ServiceConfig

config = ServiceConfig(name="orders", nats_token="t0ken")
```

### A cross-namespace listener needs a namespace

<!-- changelog.d: cross-namespace-needs-a-namespace.md -->

**You see.** Startup raises `ConfigurationError` naming the handler. A listener with
`cross_namespace=True` subscribes to `*.<pattern>`, which matches a publisher in any namespace and
never one with none, so on a service with no namespace it received nothing.

**Change.** Set `namespace` on the `ServiceConfig`, or drop `cross_namespace=True`. A service that
has a namespace is unaffected.

```text
from cliffracer import CliffracerService, ServiceConfig, listener


class Audit(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="audit"))

    @listener("orders.created", cross_namespace=True, fanout=True)
    async def on_order(self, subject: str) -> None: ...
```

```python
from cliffracer import CliffracerService, ServiceConfig, listener


class Audit(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="audit", namespace="shop"))

    @listener("orders.created", cross_namespace=True, fanout=True)
    async def on_order(self, subject: str) -> None: ...
```

### A handler is not named for a method the framework calls

<!-- changelog.d: a-handler-cannot-replace-a-method-the-framework-calls.md -->

**You see.** Startup raises `ConfigurationError` naming the handler and the method, for a decorated
handler (`@timer`, `@rpc`, `@listener` and the others) named like a method `CliffracerService`
defines, such as `health_check`. The decorator only marks the method, so the handler replaced the
framework's for the whole service: a `@timer` named `health_check` made `/health` and `/ready`
answer 500 on a healthy service.

**Change.** Rename the handler.

```text
from cliffracer import CliffracerService, ServiceConfig, timer


class Worker(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="worker"))

    @timer(interval=30)
    async def health_check(self) -> None: ...
```

```python
from cliffracer import CliffracerService, ServiceConfig, timer


class Worker(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="worker"))

    @timer(interval=30)
    async def check_backlog(self) -> None: ...
```

### A timer takes an interval the loop can run

<!-- changelog.d: a-timer-option-the-loop-cannot-honour-is-refused.md -->

**You see.** `@timer` and `Timer` raise `ConfigurationError` where the decorator is applied, which
is at import, for an `interval` that is zero, negative, not finite, a bool or not a number, and for
a `max_drift` or `error_backoff` that is negative, not finite, a bool or not a number. A zero
interval ran the method back to back with no sleep; a string passed decoration and failed inside
the loop with a `TypeError` that was logged and retried for ever.

**Change.** Give each option a finite number of seconds. A `max_drift` or `error_backoff` of `0` is
allowed.

**Your choice.** The interval itself. A zero interval asked for no pause at all; the smallest pause
you accept is yours to name.

```text
from cliffracer import CliffracerService, timer


class Poller(CliffracerService):
    @timer(interval=0)
    async def poll(self) -> None: ...
```

```python
from cliffracer import CliffracerService, timer


class Poller(CliffracerService):
    @timer(interval=0.05)
    async def poll(self) -> None: ...
```

### A service configured for msgpack has the package

<!-- changelog.d: a-service-configured-for-msgpack-without-the-package-is-refused-at-startup.md -->

**You see.** `start()` raises `ConfigurationError` before it connects, naming the service and giving
the install command, for a `ServiceConfig` with `serialization_format="msgpack"` when the `msgpack`
package is not installed. Such a service started, connected and served: it handled the JSON events
and calls sent to it, and failed with `ImportError` inside a handler or a publisher at the first
publish, call or reply that serialised in msgpack.

**Change.** Install the package, or set the format to `"json"`.

**Your choice.** Which one. The format is part of the wire contract with every other service that
talks to this one; the page does not pick it.

```text
from cliffracer import CliffracerService, ServiceConfig, listener


class Ingest(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="ingest", serialization_format="msgpack"))

    @listener("readings.created", fanout=True)
    async def on_reading(self, subject: str, value: float = 0.0) -> None: ...
```

```python
from cliffracer import CliffracerService, ServiceConfig, listener


class Ingest(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="ingest", serialization_format="json"))

    @listener("readings.created", fanout=True)
    async def on_reading(self, subject: str, value: float = 0.0) -> None: ...
```

### `start()` raises when the service was stopped while it was starting

<!-- changelog.d: start-says-so-when-the-service-was-stopped-while-starting.md -->

**You see.** `start()` raises `ServiceLifecycleError`, "was stopped while it was starting", when the
service was stopped from inside its own startup, for example by an `on_startup` that calls `stop()`.
It returned normally as if the service were up; the service ended stopped either way.
`ServiceRunner` logs the stop as a crashed start. A stop from another task still cancels `start()`,
and a caller that cancels `start()` itself, such as a startup timeout, still sees its cancellation.

**Change.** Catch `ServiceLifecycleError` where `start()` is awaited, or do not stop the service from
`on_startup`.

```text
from cliffracer import CliffracerService, ServiceConfig


class Worker(CliffracerService):
    def __init__(self, config: ServiceConfig | None = None):
        super().__init__(config or ServiceConfig(name="worker"))
        self.queue: list[str] = []

    async def on_startup(self) -> None:
        if not self.queue:
            await self.stop()


async def serve(worker: Worker) -> str:
    await worker.start()
    return "started"
```

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer.core.exceptions import ServiceLifecycleError


class Worker(CliffracerService):
    def __init__(self, config: ServiceConfig | None = None):
        super().__init__(config or ServiceConfig(name="worker"))
        self.queue: list[str] = []

    async def on_startup(self) -> None:
        if not self.queue:
            await self.stop()


async def serve(worker: Worker) -> str:
    try:
        await worker.start()
    except ServiceLifecycleError:
        return "stopped while starting"
    return "started"
```

### `stop()` returns when the broker does not answer the connection's drain

<!-- changelog.d: stop-is-bounded-by-shutdown-timeout-against-a-silent-broker.md -->

**You see.** `stop()` against a broker that has gone silent returns, where it raised nats-py's
`FlushTimeoutError`. A path to the broker that drops packets still reads as connected for minutes, so
the connection is drained, and the drain waits on an answer that does not come. It waits up to
`shutdown_timeout` seconds (no deadline when that is `None`), logs a warning that the service "could
not drain its NATS connection" and that messages still buffered may not have been sent, closes the
connection and returns. Code that caught `FlushTimeoutError` around `stop()` to learn that a stop
lost buffered messages never runs that handler. A shutdown is up to four `shutdown_timeout` periods
in the worst case: the timers' grace, the drain of active tasks, the cancellation grace and the
connection's drain. An extension's `stop()` has no deadline of its own.

**Change.** Where the exception marked a stop as unclean, read the warning in the log instead. A
process manager that kills a stopping service after a fixed time needs that time to cover the worst
case.

**Your choice.** `shutdown_timeout`, which sets each of the four periods; [Tasks that refuse
shutdown](api-reference.md#tasks-that-refuse-shutdown) says what each one covers.

```text
from nats.errors import FlushTimeoutError


async def shut_down(service) -> str:
    try:
        await service.stop()
    except FlushTimeoutError:
        return "unclean"
    return "clean"
```

```python
async def shut_down(service) -> None:
    # A drain cut off at shutdown_timeout is a warning in the log, not an exception.
    await service.stop()
```

### `ServiceConfig.nats_password` and `nats_token` are `SecretStr`

<!-- changelog.d: a-service-config-does-not-print-its-credentials.md -->

**You see.** `repr`, `model_dump_json()` and the errors a refused `ServiceConfig` raises show the
password and the token masked, and `config.nats_password` and `config.nats_token` are `SecretStr`
objects and not `str`. Code that uses one as a string raises `AttributeError` on a `str` method,
compares it to a string and gets `False`, or passes the mask on to a library that expected the
credential.

**Change.** Read the credential with `config.nats_password.get_secret_value()` or
`config.nats_token.get_secret_value()`. A plain `str` is still accepted when the config is built, so
the code that sets them does not change.

A config saved with `model_dump_json()` holds the mask `**********` for each, and a config read back
from that JSON holds the mask as its password or token and sends it to the broker, which refuses the
connection. Supply the credentials again when you read a saved config, from the environment or a
secret store.

```text
from cliffracer import ServiceConfig

config = ServiceConfig(name="orders", nats_user="orders", nats_password="s3cret")
password = config.nats_password.upper()
```

```python
from cliffracer import ServiceConfig

config = ServiceConfig(name="orders", nats_user="orders", nats_password="s3cret")
password = config.nats_password.get_secret_value().upper()
```

### A `ServiceConfig` does not print or dump the password in its `nats_url`

<!-- changelog.d: a-service-config-does-not-print-the-password-in-its-nats-url.md -->

**You see.** `repr`, `str`, a printed `model_dump()`, `__dict__` and `dict(config)` show
`nats://***@broker:4222` where the URL held `orders:s3cret@`, and `model_dump_json()` carries that
redacted URL. A config saved with `model_dump_json()` and loaded with `model_validate_json()` no
longer holds the credentials: it dials `nats://***@broker:4222`, and nats-py sends `***` as the
token. A broker that authenticates refuses the connection (`Authorization Violation`), and one that
does not authenticate accepts it, so the failure appears only against an authenticating broker. A
JSON dump masks `nats_password` and `nats_token` too, so a reload holds the mask for them as well.
`config.nats_url` is still a `str`, but it is an instance of a subclass whose `repr` is redacted:
`isinstance(config.nats_url, str)`, `==`, `str()`, an f-string and a dial use the whole URL, while
`type(config.nats_url) is str` is false and `repr()`, `%r`, `!r` and a list or dict holding it show
the redacted form.

**Change.** Supply the credentials again where a config is reloaded from JSON, by validating the
saved dictionary with the URL put back, as below. Or persist `model_dump()`, the Python dump, which
holds the URL, the password and the token whole and so is as secret as they are. Code that compares
`type(config.nats_url)` with `str`, or reads its `repr` as the URL, uses `isinstance` or
`str(config.nats_url)` instead.

**Your choice.** Where the reloaded config gets its credentials from: the place the deployment
keeps its secrets, or a Python dump stored as one.

```text
from cliffracer import ServiceConfig

url = "nats://orders:s3cret@broker:4222"
config = ServiceConfig(name="orders", nats_url=url)
reloaded = ServiceConfig.model_validate_json(config.model_dump_json())
assert reloaded.nats_url == url, "the reloaded config dials the same URL"
```

```python
import json

from cliffracer import ServiceConfig

url = "nats://orders:s3cret@broker:4222"  # from the deployment's secret store
config = ServiceConfig(name="orders", nats_url=url)
saved = config.model_dump_json()

reloaded = ServiceConfig.model_validate({**json.loads(saved), "nats_url": url})
assert reloaded.nats_url == url
assert "s3cret" not in repr(reloaded)
```

### `cliffracer run --config` refuses a key it would drop

<!-- changelog.d: cliffracer-run-config-refuses-a-key-it-would-drop-and-names-an-unused-service-section.md -->

**You see.** `cliffracer run --config` exits with status 2 and a `ConfigError` that names a
top-level key other than `global` and `services`, and says a service's settings go under
`services.<name>`. A service section written at the top level, a `service:` or a `globals:` was
dropped without a word, so the service ran without the settings in it, its credentials included.
A `services:` section for a name no service in the run has is not refused, because one file can
serve several deployments, but the command logs a warning that names the unused sections and the
services it runs.

**Change.** Put each service's settings under `services:`, keyed by the service's name, and
settings for every service under `global:`.

```text
import tempfile
from pathlib import Path

from cliffracer.cli.config import build_overrides, load_yaml_config

deploy = Path(tempfile.mkdtemp()) / "deploy.yaml"
deploy.write_text("my_service:\n  nats_user: svc\n  nats_password: s3cret\n")

settings = build_overrides("my_service", load_yaml_config(str(deploy)), {})
```

```python
import tempfile
from pathlib import Path

from cliffracer.cli.config import build_overrides, load_yaml_config

deploy = Path(tempfile.mkdtemp()) / "deploy.yaml"
deploy.write_text("services:\n  my_service:\n    nats_user: svc\n    nats_password: s3cret\n")

settings = build_overrides("my_service", load_yaml_config(str(deploy)), {})
assert settings["nats_user"] == "svc"
```

### A bad `dlq_subject` template is a `ValidationError`

<!-- changelog.d: a-bad-dlq-subject-template-is-a-validation-error.md -->

**You see.** `ServiceConfig(...)` raises a `ValidationError` that names the template, for a
`dlq_subject` that raises while it renders, such as `dlq.{service.owner}` or `dlq.{service[0].x}`.
It raised the formatter's own exception, an `AttributeError` for these, where every other bad
template was a `ValidationError`.

**Change.** A caller that caught `AttributeError` around the construction catches
`pydantic.ValidationError` now; it is also a `ValueError`. Only `{service}` and `{namespace}` are
available in a template, without attribute or index access.

```text
from cliffracer import ServiceConfig


def build(template: str) -> ServiceConfig | None:
    try:
        return ServiceConfig(name="orders", dlq_subject=template)
    except AttributeError:
        return None


config = build("dlq.{service.owner}")
```

```python
from pydantic import ValidationError

from cliffracer import ServiceConfig


def build(template: str) -> ServiceConfig | None:
    try:
        return ServiceConfig(name="orders", dlq_subject=template)
    except ValidationError:
        return None


config = build("dlq.{service.owner}")
```

### `CyanideConfig` refuses a setting it cannot carry out

<!-- changelog.d: cyanide-refuses-settings-it-cannot-carry-out.md -->

**You see.** `CyanideConfig(...)` raises a `ValidationError` that names the setting, for a `mode`
that is not one the extension knows, for a `*_weight` outside 0 to 1, and for four weights that add
up to more than 1. `CyanideExtension(...)` raises it too, as does an environment variable read
through the same model, such as `CLIFFRACER_CYANIDE_MODE=slwo` or
`CLIFFRACER_CYANIDE_SLOW_WEIGHT=1.5`. `set_mode()` and `configure_handler()` raise `ValueError`
for a mode that is not a name. The refusal comes when the object is built, and a service declares
`cyanide = CyanideExtension()` in its class body, so a service with a bad setting fails when its
module is imported and not when it starts. A misspelt `mode` was accepted and ran the whole soak with no
faults and one warning per message, while `/health` reported the mode, and weights that added up to
more than 1 gave the later modes less than the share written.

**Change.** Name a mode the extension knows (`slow`, `raise_after_delay`, `sleep_past_timeout`,
`drop_reply`, `random` or an alias) and keep each weight in 0 to 1 with the four adding up to at
most 1. A soak that read its settings from the environment fails at import until the variables are
corrected.

```text
from cliffracer import CliffracerService
from cliffracer_cyanide import CyanideConfig, CyanideExtension


class Orders(CliffracerService):
    cyanide = CyanideExtension(config=CyanideConfig(enabled=True, mode="slwo"))
```

```python
from cliffracer import CliffracerService
from cliffracer_cyanide import CyanideConfig, CyanideExtension


class Orders(CliffracerService):
    cyanide = CyanideExtension(config=CyanideConfig(enabled=True, mode="slow"))
```

## JetStream

### A durable listener has a stream that carries its subject

<!-- changelog.d: a-durable-listener-with-no-covering-stream-is-refused-at-startup.md -->

**You see.** `start()` raises `StreamDeclarationError` before it connects, so `on_startup` and the
timers have not run. It names each uncovered subject, its durable, its handler and the claims the
declared streams make. It applies to push and pull listeners alike. A listener whose subject only
another service's stream covers now fails too: the check reads this service's `jetstream_streams`,
whatever else is on the broker.

**Change.** Declare the stream the listener reads in `jetstream_streams`.

**Your choice.** Whether this service owns the stream. A stream already on the broker with other
subjects is refused after the connection, under the same rule
([ADR-0010](decisions.md#adr-0010-fail-fast-startup-validation-over-permissive-defaults)), so
declare it the way its owner did.

```text
from cliffracer import CliffracerService, ServiceConfig, listener


class Billing(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="billing", jetstream_enabled=True))

    @listener("orders.created", durable="billing")
    async def on_created(self, subject: str, order_id: str = "") -> None: ...
```

```python
from cliffracer import CliffracerService, ServiceConfig, listener
from cliffracer.core.jetstream import StreamSpec


class Billing(CliffracerService):
    def __init__(self):
        super().__init__(
            ServiceConfig(
                name="billing",
                jetstream_enabled=True,
                jetstream_streams=[StreamSpec(name="ORDERS", subjects=["orders.*"])],
            )
        )

    @listener("orders.created", durable="billing")
    async def on_created(self, subject: str, order_id: str = "") -> None: ...
```

### A stream subject does not begin with a wildcard

<!-- changelog.d: a-stream-subject-cannot-begin-with-a-wildcard.md -->

**You see.** `StreamSpec(...)` raises a `ValidationError` when it is built, for a subject that
begins with a wildcard (`*.events.x.*`, `*.>`, `>`). The message names the subject and the
server's error code `10052`. nats-server has refused these since 2.10.29, and the service failed
at startup with text that named neither. A lone `*` and a wildcard after the first token
(`a.*.>`) are accepted.

**Change.** List the namespaces the stream carries. A broker older than 2.10.29 accepted the
refused shapes; a declaration that relied on that has to enumerate them too.

```text
from cliffracer.core.jetstream import StreamSpec

orders = StreamSpec(name="ORDERS", subjects=["*.events.orders.*"])
```

```python
from cliffracer.core.jetstream import StreamSpec

orders = StreamSpec(
    name="ORDERS", subjects=["shop.events.orders.*", "wholesale.events.orders.*"]
)
```

### A stream declaration is refused when it is built

<!-- changelog.d: a-stream-declaration-is-checked-before-any-stream-is-created.md -->

**You see.** `StreamSpec(...)`, and a `ServiceConfig` that lists one in `jetstream_streams`, raise a
`ValidationError` that names the stream. It refuses a name nats-py refuses (empty, or holding a
wildcard, a dot, a slash, a backslash or white space), a subject the server refuses (an empty
token, white space, a `>` that is not the last token), two subjects of one stream that overlap or
repeat, and a `max_age_seconds` or `duplicate_window_seconds` that is negative or not finite. These
were accepted and failed when the stream was added, after the streams before it in the list were
created, as a `ValueError`, an `OverflowError` or a `ServerError` that named no declaration.
`ensure_streams` checks every declaration again where it is applied, because assigning to a field
or `model_construct` skips the check made when it is built. It raises one `StreamDeclarationError`
that names each refused stream, and creates none.

**Change.** Correct the declaration. Code that caught `ValueError`, `OverflowError` or a
`ServerError` around `ensure_streams` to find a bad declaration catches `StreamDeclarationError`
there, and a `ValidationError` where the spec is built.

```text
from cliffracer.core.jetstream import StreamSpec

orders = StreamSpec(name="ORDERS", subjects=["orders.*", "orders.created"])
```

```python
from cliffracer.core.jetstream import StreamSpec

orders = StreamSpec(name="ORDERS", subjects=["orders.*"])
```

### An explicit `idempotency_key` carries no ordinal inside `@idempotent`

<!-- changelog.d: an-explicit-idempotency-key-is-used-exactly-as-given.md -->

**You see.** Inside an `@idempotent` handler, `publish_event(..., idempotency_key=key)` sent the
`Nats-Msg-Id` `<subject>:<key>` for the first message of the call and `<subject>:<key>#<n>` for the
nth, so the id a key produced depended on how many publishes came before it. It sends
`<subject>:<key>` every time, and an explicit publish does not count towards the ordinals of the
messages the handler's own key derives. The id of the first message of a call, and of any publish
outside a decorated call, is the same as it was. An id longer than 128 bytes is still replaced by its
SHA-256 hash, and an empty key is still no key.

**Change.** Nothing, when each explicit key names one message. A handler that publishes twice to one
subject with the **same** explicit key, relying on the ordinal to store both, now deduplicates the
second against the first: give each message its own key. A retry that straddles the deploy can store
a message of the old form twice, because JetStream compares the old and new ids as different.

**Your choice.** Whether a handler names its messages itself (`idempotency_key=`, stable under any
publish order, so also under concurrent tasks) or lets `@idempotent` number them (stable only while the
handler publishes in the same order on a retry).

```text
import asyncio

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.idempotency import idempotent
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import ServiceTestHarness


class Jobs(CliffracerService):
    @rpc
    @idempotent(key="job_id")
    async def work(self, job_id: str) -> str:
        await self.publish_event("jobs.done", idempotency_key="part-a", part="a")
        await self.publish_event("jobs.done", idempotency_key="part-b", part="b")
        return "ok"


config = ServiceConfig(
    name="jobs",
    health_port=0,
    jetstream_enabled=True,
    jetstream_streams=[StreamSpec(name="JOBS", subjects=["jobs.>"]), StreamSpec(name="DLQ", subjects=["dlq.>"])],
)


async def ids():
    async with ServiceTestHarness(Jobs, config=config) as harness:
        await harness.rpc("work", job_id="j1")
        return [headers["Nats-Msg-Id"] for _, _, headers in harness.jetstream.published]


assert asyncio.run(ids()) == ["jobs.done:part-a", "jobs.done:part-b#1"], "an explicit key is used as given"
```

```python
import asyncio

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.core.idempotency import idempotent
from cliffracer.core.jetstream import StreamSpec
from cliffracer.testing import ServiceTestHarness


class Jobs(CliffracerService):
    @rpc
    @idempotent(key="job_id")
    async def work(self, job_id: str) -> str:
        await self.publish_event("jobs.done", idempotency_key="part-a", part="a")
        await self.publish_event("jobs.done", idempotency_key="part-b", part="b")
        return "ok"


config = ServiceConfig(
    name="jobs",
    health_port=0,
    jetstream_enabled=True,
    jetstream_streams=[StreamSpec(name="JOBS", subjects=["jobs.>"]), StreamSpec(name="DLQ", subjects=["dlq.>"])],
)


async def ids():
    async with ServiceTestHarness(Jobs, config=config) as harness:
        await harness.rpc("work", job_id="j1")
        return [headers["Nats-Msg-Id"] for _, _, headers in harness.jetstream.published]


assert asyncio.run(ids()) == ["jobs.done:part-a", "jobs.done:part-b"]
```

## RPC

### A service's response grant outlasts its deadline reply

<!-- changelog.d: a-response-grant-outlasts-the-deadline-reply.md -->

**You see.** `broker_permissions(..., role="service", response_ttl=...)` raises `ValueError` when
the config sets `max_rpc_processing_time` and `response_ttl` is below it plus
`RESPONSE_GRANT_MARGIN` (one second). The message names the value to raise `response_ttl` to.
A role derived without passing `response_ttl` is held to the same rule on its 30-second default,
so any `max_rpc_processing_time` above 29 seconds is now refused.

**Change.** Raise `response_ttl` to at least `max_rpc_processing_time + RESPONSE_GRANT_MARGIN`.

**Why.** The broker starts the response grant when it routes the request, before the service's
deadline starts. A grant of exactly the deadline expired before the `deadline_exceeded` reply the
service sends at it: on nats-server 2.10.29 and 2.11.2, the broker refused that reply as a
permissions violation, and the caller timed out instead of getting the answer.

```text
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.broker_permissions import broker_permissions


class Orders(CliffracerService):
    @rpc
    async def get(self) -> int:
        return 1


config = ServiceConfig(name="orders", nats_inbox_prefix="_INBOX.orders", max_rpc_processing_time=2.0)
broker_permissions(Orders, config, role="service", response_ttl=2.0)
```

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.broker_permissions import RESPONSE_GRANT_MARGIN, broker_permissions


class Orders(CliffracerService):
    @rpc
    async def get(self) -> int:
        return 1


config = ServiceConfig(name="orders", nats_inbox_prefix="_INBOX.orders", max_rpc_processing_time=2.0)
broker_permissions(Orders, config, role="service", response_ttl=2.0 + RESPONSE_GRANT_MARGIN)
```

### `call_rpc` raises the typed errors the standalone client raises

<!-- changelog.d: call-rpc-raises-the-standalone-clients-typed-errors-and-wraps-a-lost-connection.md -->

**You see.** An error reply from `self.call_rpc(...)` is `RpcValidationError` (with `.details`),
`RpcUnknownMethodError`, `RpcRefusedError` or `RpcServerError`, where it was a plain `RpcError`
whose message began `RPC Error calling <service>.<method>:`. An `RpcServerError` message begins
with the subject, and `.details` is filled for a validation error only. A connection lost before
the reply, nats' `ConnectionClosedError` or `StaleConnectionError`, is an `RpcConnectionError`
with the nats error as its `__cause__`.

`except RpcError` still matches all of them. Code that tests the message text, or catches the nats
errors, does not match and the error propagates. The circuit breaker's default set now sees a
lost connection and a server fault.

**Change.** Catch the class. The reply's `code` is what classifies it
([ADR-0011](decisions.md#adr-0011-structured-assertions-over-substring-matching)).

```text
import nats.errors

from cliffracer.core.exceptions import RpcError


async def reserve(svc, sku: str):
    try:
        return await svc.call_rpc("inventory", "reserve", sku=sku)
    except nats.errors.ConnectionClosedError:
        return "retry"
    except RpcError as exc:
        if str(exc).startswith("RPC Error calling inventory.reserve: validation failed"):
            return "invalid"
        raise
```

```python
from cliffracer.core.exceptions import RpcConnectionError, RpcValidationError


async def reserve(svc, sku: str):
    try:
        return await svc.call_rpc("inventory", "reserve", sku=sku)
    except RpcConnectionError:
        return "retry"
    except RpcValidationError as exc:
        return exc.details
```

### `call_rpc` raises `RpcNoRespondersError` when nothing is subscribed

<!-- changelog.d: a-call-to-a-service-nothing-is-subscribed-for-raises-rpc-no-responders.md -->

**You see.** `CliffracerService.call_rpc` raises `RpcNoRespondersError` when nothing is subscribed to
the target's subject. It let nats' own `NoRespondersError` through, which is not an `RpcError`: an
`except RpcError` around the call missed it, and a circuit breaker never counted it, so a service
that had gone away never opened its circuit. The standalone client already raised
`RpcNoRespondersError`. Code that catches `nats.errors.NoRespondersError` around `call_rpc` does not
match, and the error propagates; the nats error is the new one's `__cause__`.

**Change.** Catch `RpcNoRespondersError`, or `RpcError` for every failure of the call.

```text
import nats.errors


async def notify(svc, sku: str):
    try:
        return await svc.call_rpc("inventory", "reserve", sku=sku)
    except nats.errors.NoRespondersError:
        return "gone"
```

```python
from cliffracer.core.exceptions import RpcNoRespondersError


async def notify(svc, sku: str):
    try:
        return await svc.call_rpc("inventory", "reserve", sku=sku)
    except RpcNoRespondersError:
        return "gone"
```

### A send raises an `RpcError` for what the connection raises

<!-- changelog.d: the-client-maps-every-nats-error-on-the-request-path.md -->
<!-- changelog.d: the-service-sends-map-nats-errors.md -->

**You see.** A `ServiceClient` call, and a service's `call_rpc`, `call_async`,
`call_rpc_no_wait`, `publish_event` and `broadcast_message`, raise an `RpcClientError` for an
argument larger than the broker's `max_payload`, and an `RpcConnectionError` for a full outbound
buffer during a reconnect, a draining connection and a closed or stale one, where they raised
nats-py's `MaxPayloadError`, `OutboundBufferLimitError` and `ConnectionDrainingError`. The nats-py
error is the `__cause__`. A JetStream error about the stream (no stream answered, say) is not mapped
and reaches the caller as it was.

`except RpcError` matches all of them. Code that catches the nats-py errors around these calls
does not match, and the error propagates. A service that has no connection
at all is a different case, covered by the next entry.

**Change.** Catch the class. An oversized argument is the caller's, so it is the `RpcClientError`;
the others are failures of the connection.

```text
import nats.errors


async def charge(svc, amount: int):
    try:
        return await svc.call_rpc("billing", "charge", amount=amount)
    except nats.errors.MaxPayloadError:
        return "too large"
```

```python
from cliffracer.core.exceptions import RpcClientError


async def charge(svc, amount: int):
    try:
        return await svc.call_rpc("billing", "charge", amount=amount)
    except RpcClientError:
        return "too large"
```

### A request without its rate-limit key is refused, not answered `internal`

<!-- changelog.d: a-default-rate-limit-is-for-callers-and-a-missing-key-is-a-refusal.md -->

**You see.** A request that lacks the header or payload field its handler's `@rate_limit` is keyed
on is answered `refused: rate-limit key 'client' is missing from the declared payload source`,
naming the key, and `/health` counts it among that handler's refusals. A caller's `call_rpc` and a
`ServiceClient` call raise `RpcRefusedError` for it, where they raised `RpcServerError`: the reply
was `internal`, and the service logged an error per request. An `except RpcServerError` around the
call does not catch it, and a circuit breaker's default set does not count it. A durable
event that lacks the field is acknowledged, where it was redelivered up to `max_deliver` and then
dead-lettered.

With `default_calls=` and `default_window=`, a `describe` request and a timer or cron firing are no
longer counted against the default limit; a handler that declares its own `@rate_limit` keeps it
whatever its kind.

**Change.** Send what the limit is keyed on, the header or the payload field it names, and catch
`RpcRefusedError` for a refusal. `RpcRefusedError.reason` says which key is missing.

```text
from cliffracer.core.exceptions import RpcServerError


async def search(svc, query: str):
    try:
        return await svc.call_rpc("catalog", "search", query=query)
    except RpcServerError:
        return "try again later"
```

```python
from cliffracer.core.exceptions import RpcRefusedError


async def search(svc, query: str, client: str = "web"):
    # catalog.search limits per payload field "client"
    try:
        return await svc.call_rpc("catalog", "search", query=query, client=client)
    except RpcRefusedError as refusal:
        return refusal.reason
```

### A declared extension runs before the payload is validated

<!-- changelog.d: validation-runs-after-the-extensions-a-service-declares.md -->

**You see.** `ValidationExtension` runs after the extensions a service declares, not before them.
An RPC or async RPC whose payload decodes but is invalid is turned away by a declared gate first:
an unauthenticated caller gets `refused: unauthenticated`, raised as `RpcRefusedError`, where it
got `validation_failed` with the field errors and the rejected input, raised as
`RpcValidationError`. A rate limit spends a permit on a payload that validation then refuses, and
the service's own validators do not run on input a gate refused. A payload that cannot be decoded,
a body that is not JSON or msgpack, is still answered `validation_failed` before any declared
extension runs.

A declared extension's `worker_setup` that reads `ctx.data["validated_kwargs"]` raises
`KeyError`, because the arguments are set after it. An extension with `fails_closed = True`
refuses every RPC as `internal`; any other has the error logged and carries on without its check.

**Change.** Read the payload as it arrived, `ctx.payload`, and do not assume it is valid, or move
the read to `worker_result`, where `ctx.data["validated_kwargs"]` is set. A caller that read
`.details` off an `RpcValidationError` for an unauthenticated request authenticates first.

```text
from cliffracer import CliffracerService, Extension, RejectMessage, rpc


class CapGate(Extension):
    fails_closed = True

    async def worker_setup(self, ctx):
        if ctx.data["validated_kwargs"]["amount"] > 1000:
            raise RejectMessage("over the cap")


class Billing(CliffracerService):
    cap = CapGate()

    @rpc
    async def charge(self, amount: int) -> int:
        return amount
```

```python
from cliffracer import CliffracerService, Extension, RejectMessage, rpc


class CapGate(Extension):
    fails_closed = True

    async def worker_setup(self, ctx):
        amount = ctx.payload.get("amount") if isinstance(ctx.payload, dict) else None
        if isinstance(amount, int) and amount > 1000:
            raise RejectMessage("over the cap")


class Billing(CliffracerService):
    cap = CapGate()

    @rpc
    async def charge(self, amount: int) -> int:
        return amount
```

### A reply carries no request headers

<!-- changelog.d: an-rpc-reply-does-not-echo-the-request-headers.md -->

**You see.** An RPC reply, and the reply to a `describe` request, carries only its
`Content-Type` and the `X-Correlation-ID` the service used. The headers of the request are not on
it: a caller's `Authorization` token, its own `X-Tenant` and a correlation id the service refused
came back on every reply, and `reply.headers["X-Tenant"]` now raises `KeyError`. The error and
refusal fields are in the envelope, as before.

**Change.** Read a request header from your own request, which you still hold. The correlation id
the service used for the request is `X-Correlation-ID` on the reply.

```text
async def tenant_of(nc, subject: str) -> str:
    reply = await nc.request(subject, b"{}", headers={"X-Tenant": "acme"})
    return reply.headers["X-Tenant"]
```

```python
async def tenant_of(nc, subject: str) -> tuple[str, str]:
    tenant = "acme"
    reply = await nc.request(subject, b"{}", headers={"X-Tenant": tenant})
    return tenant, reply.headers["X-Correlation-ID"]
```

### A handler parameter takes no alias

<!-- changelog.d: a-handler-parameter-alias-is-refused-at-discovery.md -->

**You see.** A service that declares a handler parameter with a Pydantic alias, `Field(alias=...)`,
`validation_alias` or `serialization_alias` in its `Annotated` metadata, on an `@rpc` handler or an
event listener, fails to start. Startup raises `UntypedHandler` naming the handler and the
parameter, before the service connects, and `describe` raises it too. Where the service started,
described the parameter as `item` and accepted it only as `itemId`, a generated client or
`RpcProxy` call, which sends `item`, was answered `validation_failed` with `missing` for `itemId`,
and a caller that sent `itemId` by hand was the one that worked. That caller now has no service to
call.

**Change.** Remove the alias. A handler parameter is described, called and accepted by its Python
name, so a caller that sent `itemId` sends `item`.

```text
from typing import Annotated

from pydantic import Field

from cliffracer import CliffracerService, rpc


class Cart(CliffracerService):
    @rpc
    async def take(self, item: Annotated[int, Field(alias="itemId")]) -> int:
        return item
```

```python
from cliffracer import CliffracerService, rpc


class Cart(CliffracerService):
    @rpc
    async def take(self, item: int) -> int:
        return item
```

### A send with no connection raises `RpcConnectionError` or `ServiceLifecycleError`

<!-- changelog.d: a-send-without-a-connection-raises-a-named-error-before-its-hooks-run.md -->

**You see.** `call_rpc`, `call_async`, `call_rpc_no_wait`, `publish_event` and `broadcast_message` on
a service that has no connection raise an error that names the service and the subject, before any
`before_call` hook runs. The three calls raise `RpcConnectionError`, as the standalone client does
for a connection that is not there. The two publishes raise `ServiceLifecycleError`, which is a
`RuntimeError` and not an `RpcError`; so does a publish on a service with `jetstream_enabled` and no
JetStream context. A connection that exists and has closed, is draining or is stale is the other
case and has one class: all five raise `RpcConnectionError`, the nats-py error as its `__cause__`
(see the entry above). These sends raised a bare `AssertionError`, or `AttributeError` under
`python -O`, after the hooks had run, so a handler for `RpcError` missed them.

**Change.** Catch both classes where a send can run on a service that is not connected.
`except RpcError` holds every `RpcConnectionError` and not the `ServiceLifecycleError`.

```text
async def notify(svc, order_id: str) -> str:
    try:
        await svc.publish_event("orders.created", order_id=order_id)
    except AssertionError:
        return "not connected"
    return "sent"
```

```python
from cliffracer.core.exceptions import RpcConnectionError, ServiceLifecycleError


async def notify(svc, order_id: str) -> str:
    try:
        await svc.publish_event("orders.created", order_id=order_id)
        await svc.call_rpc("billing", "invoice", order_id=order_id)
    except ServiceLifecycleError:
        return "not connected"
    except RpcConnectionError:
        return "connection lost"
    return "sent"
```

### `close()` and leaving `async with` release a connection that is reconnecting

<!-- changelog.d: closing-a-client-releases-its-connection-however-it-is-reconnecting.md -->

**You see.** `ServiceClient.close()` and the exit of `async with client` return while the broker is
reconnecting, where they raised nats-py's `ConnectionReconnectingError`. nats-py refuses a drain
while it redials, which is when a shutdown path runs, during a broker outage. The client is marked
closed first and a connection that cannot be drained is closed instead; a failure to release is
logged and not raised. The block's own exception is the one that leaves `async with`. A handler for
`ConnectionReconnectingError` around either never runs, and a client that was closed refuses later
calls with `RpcConnectionError`.

**Change.** Drop the handler. There is nothing to retry: when `close()` returns the connection is
closed. A connection the client was handed is left to its owner, as before.

```text
import nats.errors


async def shut_down(client) -> str:
    try:
        await client.close()
    except nats.errors.ConnectionReconnectingError:
        return "still open"
    return "closed"
```

```python
async def shut_down(client) -> None:
    await client.close()
```

### A failed re-verify does not replace a validation error

<!-- changelog.d: the-client-keeps-a-callers-correlation-id-validates-its-subject-and-reports-why.md -->

**You see.** When a `ServiceClient` call comes back with a validation reply, the client describes the
service again to learn whether its contract moved. A call whose describe then fails raises the
`RpcValidationError` the service sent, where it raised the describe's own `RpcTimeoutError` or
`RpcConnectionError`; the describe's error is logged at debug level. A describe that finds the
contract moved still raises `ClientOutOfDate`. Code that took the timeout or the connection error
from such a call to mean that the service is down receives the validation error instead, whose
`.details` say which argument was wrong. The same entry changes four other things about the client,
which [CHANGELOG.md](../CHANGELOG.md) lists; one is a `ValueError` when a client is built with a
`service`, `namespace` or `subject_prefix` that `ServiceConfig` would refuse.

**Change.** Catch `RpcValidationError` where the arguments are the question, and keep the timeout and
connection handlers for a call that failed itself.

```text
from cliffracer.core.exceptions import RpcConnectionError, RpcTimeoutError


async def reserve(client, sku: str):
    try:
        return await client.reserve(sku=sku)
    except (RpcTimeoutError, RpcConnectionError):
        return "service unavailable"
```

```python
from cliffracer.core.exceptions import RpcConnectionError, RpcTimeoutError, RpcValidationError


async def reserve(client, sku: str):
    try:
        return await client.reserve(sku=sku)
    except RpcValidationError as exc:
        return exc.details
    except (RpcTimeoutError, RpcConnectionError):
        return "service unavailable"
```

### A generated client is regenerated for a model default

<!-- changelog.d: a-generated-client-builds-the-model-a-default-stands-for.md -->

**You see.** A generated client of a service with a method whose parameter default holds a model
(`line: Line = Line(sku="a1")`, or a list or dict of models) raises `ClientOutOfDate` on its first
call, naming the method, with "regenerate it" in the message. The service's description carries a
`rebuildable` key on each such parameter, which changes the method's `signature_hash` and the
description hash, and the `SIGNATURES` table written into the client does not match it. The same
holds the other way: a client regenerated from an upgraded service reports a replica that has not
been upgraded as out of date. A method with no such parameter keeps its hash, and a client built
with `verify=False` is not checked. `cliffracer-generate-client --check` reports a checked-in client
of such a service as stale, exit code 8.

**Change.** Regenerate the client with the command that wrote it, after the service is upgraded, and
check the new file in: `cliffracer-generate-client --class myapp.warehouse:Warehouse --service
warehouse --version 1.0.0 --out warehouse_client.py`, or the form with `--nats-url` against the
running service. The regenerated client builds the model for a default the service marks rebuildable
(`Line.model_validate({...}, strict=False)`) and leaves the others as the dict.
[Generating a client](api-reference.md#generating-a-client) has the options.

```text
from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer.introspect import describe


class Line(BaseModel):
    sku: str
    qty: int = 1


class Warehouse(CliffracerService):
    @rpc
    async def create(self, line: Line = Line(sku="a1")) -> int:
        return 1


(line,) = describe(Warehouse).method("create").params
assert "rebuildable" not in line.to_dict(), "the description holds only the default's dump"
```

```python
from cliffracer.generate_client.cli import main

exit_code = main(
    [
        "--class",
        "myapp.warehouse:Warehouse",
        "--service",
        "warehouse",
        "--version",
        "1.0.0",
        "--out",
        "warehouse_client.py",
    ]
)
assert exit_code == 0
```

### A return model's contract hash is the hash of what it writes

<!-- changelog.d: the-contract-hash-covers-what-a-return-model-writes.md -->

**You see.** The hash of a return model, and with it the method's `signature_hash` and the
description hash, is taken from the model's serialization-mode JSON Schema, what the handler writes,
where it was taken from the validation schema, what a caller may send. The hashes move for a return
model whose two schemas differ: one with a `computed_field` or a `serialization_alias`, a `Decimal`
field (written as a string, read as a number or a string), a `Json[...]` field, or a
`field_serializer` that changes a field's type. `Decimal` is the common case. A return model whose
two schemas are the same, and every parameter model, hash as before. A generated client of such a
service raises `ClientOutOfDate` on its first call until it is regenerated, and
`cliffracer-generate-client --check` reports it as stale. A template whose handlers return such a
model has a different RPC contract, so registering it again under a revision that holds the old
contract raises `ActivationConflict`.

**Change.** Regenerate the clients, as in the entry above. Register each such template under a new
`revision`; [Service templates](service-templates.md#contracts-and-activation-values) says a changed
declaration needs one.

```text
from decimal import Decimal

from pydantic import BaseModel

from cliffracer import CliffracerService, rpc
from cliffracer.core.typed_rpc import type_ref
from cliffracer.introspect import describe


class Receipt(BaseModel):
    total: Decimal


class Shipments(CliffracerService):
    @rpc
    async def receipt(self) -> Receipt:
        return Receipt(total=Decimal("1.50"))


returns = describe(Shipments).method("receipt").returns
assert returns["schema_hash"] == type_ref(Receipt)["schema_hash"], "a reply is hashed as a request"
```

```python
from decimal import Decimal

from pydantic import BaseModel

from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.runners.templates import ServiceTemplate, TemplateCatalog


class Receipt(BaseModel):
    total: Decimal


class Settings(BaseModel):
    warehouse: str = "north"


class Shipments(CliffracerService):
    @rpc
    async def receipt(self) -> Receipt:
        return Receipt(total=Decimal("1.50"))


def make_shipments(settings: Settings, config: ServiceConfig) -> Shipments:
    return Shipments(config)


catalog = TemplateCatalog()
template = catalog.register(
    ServiceTemplate(
        name="shipments",
        revision="warehouse-b",
        service_class=Shipments,
        settings_model=Settings,
        factory=make_shipments,
    )
)
```

### `cliffracer.core.typed_rpc.python_type` is gone

<!-- changelog.d: python-type-is-removed-type-ref-stays.md -->

**You see.** `ImportError` for `python_type`, the inverse of `type_ref`. `type_ref` is unchanged.

**Change.** A generator or client that needs the inverse builds it from the TypeRef kinds
`scalar`, `model`, `list`, `dict`, `optional` and `literal`, the `stream` a method's `returns` is
for a handler that streams its reply, and the `constraints` a ref may carry.
The consumer is yours, so it holds only what yours needs.

**Your choice.** What the consumer builds for a `model`: this one imports it by module and name,
where a client that cannot import the model builds another.

```text
from cliffracer.core.typed_rpc import python_type, type_ref

annotation = python_type(type_ref(list[int]))
```

```python
import importlib
from collections.abc import AsyncIterator
from typing import Annotated, Any, Literal

from pydantic import Field

SCALARS = {"int": int, "str": str, "float": float, "bool": bool}


def annotation(ref: dict[str, Any]) -> Any:
    kind = ref["kind"]
    if kind == "scalar":
        base = SCALARS[ref["name"]]
        constraints = ref.get("constraints")
        return Annotated[base, Field(**constraints)] if constraints else base
    if kind == "list":
        return list[annotation(ref["item"])]
    if kind == "dict":
        return dict[str, annotation(ref["value"])]
    if kind == "optional":
        return annotation(ref["inner"]) | None
    if kind == "literal":
        return Literal[tuple(ref["values"])]
    if kind == "model":
        return getattr(importlib.import_module(ref["module"]), ref["qualname"])
    if kind == "stream":
        return AsyncIterator[annotation(ref["item"])]
    raise ValueError(f"no annotation for a {kind} ref")
```

### A value no form carries is refused before it is sent

<!-- changelog.d: a-value-no-form-carries-is-refused-before-sending.md -->

**You see.** A generated client, `call_rpc`, `call_async`, `call_rpc_no_wait`, an `RpcProxy` call,
`publish_event` and `broadcast_message` raise `RpcValidationError` before anything is sent, with the message "refused before sending:
Chained would arrive with a read as b's value, b read as c's value, and no form of it is read back
as the argument".
`.details` holds one entry per field: `value_would_be_lost` for a field the receiver would read as
its default, `value_would_be_misread` for one it would read as any other value (the message names
another field when the value is that field's). It is
raised for a model argument that no form the client can write reads back as itself, where the form
it would send makes the receiver read a field the caller set as something else. These shapes do
that: an `AliasChoices` whose first member is another field's name, an `AliasPath` whose head is
another field's name, a chain of aliases each naming the next field, and a nested model whose alias
is another field's name. Without an annotation (`call_rpc`, the publishers and the rest) the call
is refused only when no model class of the argument's hierarchy reads the form, since the handler or
listener may declare one that does.

**Why the old value was wrong.** These calls completed and the handler or listener ran with a value
the caller did not pass, and nothing reported it. Above, `a` arrived holding `b`'s value and `b` holding `c`'s. A field with a
default arrived as the default.

**Change.** Give each such field an alias that is no other field's name, or an `AliasChoices` whose
first member is its own key, so a form exists that the receiver reads back. The handler and the
caller must change together, since the key on the wire changes. Where a refusal names a nested
field (`item.x`), the alias to change is on that model.

```text
import asyncio

from pydantic import BaseModel, Field

from cliffracer.client import ServiceClient


class Chained(BaseModel):
    a: int = Field(0, alias="b")
    b: int = Field(0, alias="c")
    c: int = 0


class ShopClient(ServiceClient):
    SERVICE = "shop"

    async def put(self, item: Chained) -> str:
        return await self._call("put", {"item": self._encode(item, Chained)}, str)


value = Chained.model_validate({"a": 1, "b": 2, "c": 3}, by_name=True, by_alias=False)
asyncio.run(ShopClient(verify=False).put(value))
```

```python
from pydantic import BaseModel, Field

from cliffracer.client import ServiceClient


class Chained(BaseModel):
    a: int = Field(0, alias="a_in")
    b: int = Field(0, alias="b_in")
    c: int = 0


class ShopClient(ServiceClient):
    SERVICE = "shop"

    async def put(self, item: Chained) -> str:
        return await self._call("put", {"item": self._encode(item, Chained)}, str)


value = Chained.model_validate({"a": 1, "b": 2, "c": 3}, by_name=True, by_alias=False)
```

## Auth

### `AuthConfig.secret_key` is a `SecretStr`

<!-- changelog.d: the-auth-config-does-not-print-the-signing-secret.md -->

**You see.** `repr`, `str`, `model_dump()` and `model_dump_json()` of an `AuthConfig`, and any log
line that carries one, show `**********` where the key was. Code that uses `config.secret_key` as a
`str` raises `AttributeError` on a `str` method, or passes the mask on to a library that expected
the key.

**Change.** Read the key with `config.secret_key.get_secret_value()`. A plain `str` is still
accepted at construction and by assignment, so `auth.config.secret_key = new_key` still rotates
the key; `AuthConfig` validates on assignment, so an out-of-range `pbkdf2_iterations` assigned
later is refused too.

```text
from cliffracer_auth import AuthConfig

config = AuthConfig(secret_key="0123456789abcdef0123456789abcdef")
signing_key = config.secret_key.encode()
```

```python
from cliffracer_auth import AuthConfig

config = AuthConfig(secret_key="0123456789abcdef0123456789abcdef")
signing_key = config.secret_key.get_secret_value().encode()
```

### `AuthConfig.algorithm` is one the service can sign with

<!-- changelog.d: auth-config-algorithm-is-one-the-service-can-sign-with.md -->

**You see.** `AuthConfig(...)` raises a `ValidationError` when it is built, or when the field is
assigned, for an `algorithm` other than `HS256`, `HS384` or `HS512`: `none`, an asymmetric
algorithm such as `RS256`, a lower-case `hs256` and a typo. These were accepted, the service
started, and the first login raised. With `none` the login and `validate_token` both raised.

**Change.** Use one of the three. A service signs and verifies with the shared `secret_key`, so an
algorithm that needs a key pair has no key to use.

**Your choice.** Which of the three. `HS256` is the default.

```text
from cliffracer_auth import AuthConfig

config = AuthConfig(secret_key="0123456789abcdef0123456789abcdef", algorithm="hs256")
```

```python
from cliffracer_auth import AuthConfig

config = AuthConfig(secret_key="0123456789abcdef0123456789abcdef", algorithm="HS512")
```

### A refresh stops 30 days after the login

<!-- changelog.d: refresh-is-bounded-to-thirty-days-from-the-login-by-default.md -->

**You see.** `AuthConfig.refresh_max_lifetime_hours` is `720` where it was `None`. A token is not
refreshed once 30 days have passed since the login that began its chain: `refresh_token` returns
`None` and logs a warning. Refresh is a re-issue, so the token it is given stays valid to its own
expiry.

**Change.** A deployment whose sessions are meant to outlive a month sets the field.

**Your choice.** The number of hours, or `None`, which removes the cap. The default bounds how long
a leaked token can be kept alive by refreshing it; the field is described under
[Authentication](api-reference.md#authentication).

```text
from cliffracer_auth import AuthConfig

config = AuthConfig(secret_key="0123456789abcdef0123456789abcdef")
assert config.refresh_max_lifetime_hours is None, "a refresh has no cap unless one is set"
```

```python
from cliffracer_auth import AuthConfig

config = AuthConfig(secret_key="0123456789abcdef0123456789abcdef", refresh_max_lifetime_hours=None)
```

### A token longer-lived than this service's own is refused

<!-- changelog.d: a-token-longer-lived-than-this-service-allows-is-refused.md -->

**You see.** `validate_token`, `refresh_token`, `AuthExtension` and the auth middleware refuse a
token whose `exp` is more than `token_expiry_hours` (plus `leeway_seconds`) after its `iat`, and a
token with no `iat`. Tokens this library mints are not affected: each lives exactly
`token_expiry_hours` and carries an `iat`. A token minted by another host sharing the key with a
larger `token_expiry_hours`, or by your own code without an `iat`, is refused as unauthenticated.

**Change.** Give every host that shares the key the same `token_expiry_hours` and
`refresh_max_lifetime_hours` (or let each verifying host's be at least as large as any minting
host's), and mint tokens with an `iat`. With `refresh_max_lifetime_hours=None` a revoked chain is
kept for the life of the process, so memory grows with the number of revocations.

**Why.** A revoked chain is held until a lifetime past its refresh cap, since another host does not
see the revocation and may go on refreshing it. A token that lived longer could be accepted again
once the revocation was forgotten.

```text
import time, jwt

key = "the-key-every-host-shares-at-least-32-characters"
now = time.time()
token = jwt.encode({"jti": "j1", "user_id": "u1", "username": "bob", "email": "bob@example.com",
                    "exp": now + 48 * 3600}, key, algorithm="HS256")  # no iat, 48 h
```

```python
import time, jwt

key = "the-key-every-host-shares-at-least-32-characters"
now = time.time()
token = jwt.encode({"jti": "j1", "user_id": "u1", "username": "bob", "email": "bob@example.com",
                    "iat": now, "exp": now + 24 * 3600}, key, algorithm="HS256")  # 24 h, as configured
```

## Key-Value

### `BucketConfig` and `ObjectStoreConfig` check their options when built

<!-- changelog.d: a-bucket-config-that-cannot-work-is-refused-when-built.md -->

**You see.** `BucketConfigError`, naming the bucket and the field, when the config is built. A
`ttl` that is a bool or a string, negative, not finite or under 100 ms; a `history` outside 1 to
64; `replicas` under 1; a `storage` other than `"file"`, `"memory"` or a `StorageType`. These
reached the broker and failed there with an unrelated error, or created a different bucket than
asked for (`history=0` kept every revision). A dictionary with an option the config does not have
is refused, naming it. A `ttl` of `None` in a dictionary takes the `bucket_ttls` value, as it does
in an instance, and `normalize_ttl_seconds` does not read a numeric string as seconds.

**Change.** Give each option a value in range.

```text
from cliffracer_kv import BucketConfig

sessions = BucketConfig(name="sessions", history=0)
```

```python
from cliffracer_kv import BucketConfig

sessions = BucketConfig(name="sessions", history=1, ttl=3600)
```

### A KV declaration that cannot work is refused by name

<!-- changelog.d: a-kv-declaration-that-cannot-work-is-refused-by-name.md -->

**You see.** `BucketConfigError`, naming the bucket and the option, where a declaration was taken
as something else or failed later with an error that was not a `KvError`. A bucket declared twice
with different options is refused, where the last one was kept. A declaration that is not a name,
a configuration or a dictionary of options is refused, and so is a name with a dot, a typo in a
nested `placement` or `republish` dictionary, a non-numeric `max_value_size`, a non-bool `direct`
and a `limit_marker_ttl` that is not whole seconds; these are refused when `KvExtension` reads its
declarations at setup, or when the configuration is built. A `buckets` or `object_stores` that is
neither a declaration nor a collection of them, such as `42` or `b"sessions"`, is refused when
`KvExtension` is built. A single name, configuration or dictionary passed as `buckets=` is one
bucket: `buckets="profiles"` declares `profiles`, not eight one-letter buckets.

**Change.** Declare each bucket once, with the options you want, and pass a name, a configuration,
a dictionary or a list of them.

```text
from cliffracer_kv import BucketConfig, KvExtension

extension = KvExtension(
    buckets=[BucketConfig(name="sessions", history=5), BucketConfig(name="sessions", history=1)]
)
```

```python
from cliffracer_kv import BucketConfig, KvExtension

extension = KvExtension(buckets=[BucketConfig(name="sessions", history=5)])
```

### A KV write stores JSON or raises

<!-- changelog.d: a-kv-value-with-no-json-form-is-refused.md -->
<!-- changelog.d: kv-writes-store-json-or-raise-the-same-way-for-every-value.md -->

**You see.** `put`, `create` and `put_object` raise `TypeError` naming the type for a value with no
JSON form. `put` and `create` stored the text of its `repr` and returned a revision as though the
write had worked; `put_object` raised nats-py's `TypeError: nats: invalid type for object store`,
which names no type. A dataclass, datetime, date, UUID, decimal, enum, set or tuple, alone or inside
a dict or list, is stored as JSON by all three, where `put_object` raised for one on its own.

`NaN` and infinity, which are not JSON, raise `TypeError` too, where they were written as `NaN`. So
does an iterator, a generator or a file object inside a dict or list, and for `put` and `create` on
its own as well, where it was stored as the array of the items it yielded and left used up.
`put_object` still streams a file object given on its own.

**Change.** Convert a value of your own class to a Pydantic model, a dataclass or a dict. A value
stored earlier as `repr` text is text, not JSON. Read a file's contents before `put` or `create`, or
hand the file to `put_object`; replace a `NaN` or infinity with a value JSON has, `None` or a string
say.

```text
from cliffracer_kv import KvExtension


class Job:
    id = "j1"


async def save(kv: KvExtension) -> int:
    return await kv.put("jobs", "j1", Job())
```

```python
from dataclasses import dataclass

from cliffracer_kv import KvExtension


@dataclass
class Job:
    id: str


async def save(kv: KvExtension) -> int:
    return await kv.put("jobs", "j1", Job(id="j1"))
```

### `delete()` and `purge()` return `None`

<!-- changelog.d: delete-and-purge-return-none.md -->

**You see.** `KvExtension.delete()` and `purge()` return `None`. They returned `True` every time,
and the `False` for a key that was not found, which the docstrings promised, never happened: each
writes a marker whether or not the key exists. A caller that tested the result now takes the other
branch every time. `KvError` covers the errors the package raises itself; nats-py's own errors,
such as a stale revision, propagate unchanged.

**Change.** Read the key first when the caller needs to know whether it existed.

```text
from cliffracer_kv import KvExtension


async def forget(kv: KvExtension, key: str) -> None:
    removed = await kv.delete("sessions", key)
    assert removed is True, "delete() says whether the key was there"
```

```python
from cliffracer_kv import KvExtension


async def forget(kv: KvExtension, key: str) -> bool:
    existed = await kv.get("sessions", key) is not None
    await kv.delete("sessions", key)
    return existed
```

### A bucket is declared on the extension, not on a config subclass

**You see.** Nothing at startup, and then a bucket that is not the one you declared.
`KvExtension` reads no `kv_buckets` attribute off the service's config: `ServiceConfig` has no such
field, and a config subclass that added one was never documented. A bucket named only there is not
provisioned when the service starts and none of its options (`history`, `ttl`, `replicas`,
`storage`) are applied. The first call that names the bucket creates it with the defaults, or opens
a bucket already on the broker as it is. The check that `get()` made for a deleted entry could not
run, because nats-py raises for a deleted key before an entry comes back, so `get()` reads the same.

**Change.** Declare each bucket on the extension, with the options it had on the config.

```text
from cliffracer import ServiceConfig
from cliffracer_kv import BucketConfig


class OrdersConfig(ServiceConfig):
    kv_buckets: list[BucketConfig] = [BucketConfig(name="sessions", history=5)]
```

```python
from cliffracer_kv import BucketConfig, KvExtension

extension = KvExtension(buckets=[BucketConfig(name="sessions", history=5)])
```

### A KV write refuses a model its class does not read back as itself

<!-- changelog.d: a-kv-write-stores-a-model-only-in-a-form-it-reads-back-as-itself.md -->

**You see.** `put()`, `create()` and `put_object()` raise `ModelDoesNotReadBackError` for a Pydantic
model that its own class reads back as other values than it holds from every form it can be
written in (its field-name JSON, its alias JSON, and the JSON with each field where its validation
alias reads it), and nothing is written. The error names the model and what each form read
back as. It is a `KvError` and a `TypeError`, so code that catches `TypeError` around a write
catches it. Models that read back as themselves are stored as they were. A model whose fields'
aliases are each other's field names, which read back swapped under the field names, is stored
under its aliases and reads back equal. A model read only through an `AliasChoices` or `AliasPath`
that names keys the model does not write, which read back as its defaults, is stored with each such
field where its validation alias reads it (its first `AliasChoices` member, the structure its
`AliasPath` names), and a tree whose parent reads only field names and whose child only aliases is
stored one model at a time, each in the form its class reads; both read back equal.

"Reads back as the model" is judged by declared type, over what a dump writes. A value at a typed
position must read back as the value it held, NaN as NaN, so `dict[str, datetime]` is compared
value by value. A value at a position declared `Any` or `object`, at any depth (a field, a
`list[Any]` or `tuple[Any, ...]` item, a `dict[str, Any]` value, an `Optional[...]` or union arm, a
bare `dict` or `list`; also written through a type alias such as `type Payload = dict[str, Any]`, or
as a `TypeVar` with no bound, which a model validates as `Any`), and every extra, must read back
with the same JSON form, which is all such a position promises: a `datetime` held there is stored
and reads back as its ISO string, as it did. A `TypeVar` with a bound is judged as its bound, and
one with constraints as the union of them. A value in a union is judged under every arm it is an
instance of, so it is compared by its JSON form only where each such arm promises no more:
`dict[str, Colour] | dict[str, Any]` holding an enum is compared by value, and refused, since the
enum reads back as its string.
A model held by an `Any` position has its alias dump as its JSON form, so the holder is stored
under its aliases. A private attribute and a field declared `exclude=True` are not stored, and are
not compared.

A base class that declares the model's fields is a reader too, since `get(as_type=Base)` reads the
same bytes. The form stored is one that the model's own class and every such base read back as the
model, where one exists. Otherwise it is the form earlier releases stored, if the model's own class
reads that back, so every reader reads what it read before. Otherwise it is a form no base reads
worse than it read that one. Otherwise the write is refused: a model whose subclass reads only an
alias while its base reads only the field name, for example, is refused rather than stored in a form
the base would read as its defaults.
A *required* `exclude=True` field leaves a dump the class cannot read back, so that model is refused.

The models refused are the ones whose typed fields read back changed: a `field_serializer` that
changes a typed field's value (it read back changed), a strict model with a `before` validator over
a field whose JSON form strict mode refuses (`get` raised), and a model that reads a field only
through a validation alias while a base class it inherits the field from reads it by name (the base
read the field-name form, and the model read it as its default).

**Change.** Make the model hold what it writes: move a transformation from a `field_serializer`
into a validator, and let a field read through a validation alias also read the name its base
class reads (`AliasChoices("xx", "x")`).

**Your choice.** Whether lossy storage is what you meant. If it is, store the dump yourself,
`model.model_dump_json()` or a dict, and read it back as that.

```text
from cliffracer_kv import KvExtension
from pydantic import BaseModel, field_serializer


class Job(BaseModel):
    state: str

    @field_serializer("state")
    def _upper(self, value: str) -> str:
        return value.upper()


async def save(kv: KvExtension) -> int:
    return await kv.put("jobs", "j1", Job(state="done"))
```

```python
from cliffracer_kv import KvExtension
from pydantic import BaseModel, field_validator


class Job(BaseModel):
    state: str

    @field_validator("state")
    @classmethod
    def _upper(cls, value: str) -> str:
        return value.upper()


async def save(kv: KvExtension) -> int:
    return await kv.put("jobs", "j1", Job(state="done"))
```

## Resilience

### A circuit breaker config is checked when it is built

<!-- changelog.d: a-circuit-breaker-config-that-cannot-work-is-refused-when-built.md -->

**You see.** `CircuitBreakerConfig(...)` raises `ValueError` for a `failure_threshold` or
`half_open_max_calls` below 1 and for a negative or NaN `recovery_timeout`, and `TypeError` for a
`monitored_exceptions` that is not a tuple or list of exception classes. A threshold of 0 tripped
on the first failure, no probes meant the circuit could never close, and a bare exception class
raised `TypeError` at the first exception, far from the mistake. `ResilientRpcProxy` given both
`circuit_breaker=` and `config=` raises `ValueError`, where `config=` was ignored.

**Change.** Correct the value, put a single exception class in a tuple, and pass the config or the
breaker, not both.

```text
from cliffracer_resilience import CircuitBreakerConfig

config = CircuitBreakerConfig(failure_threshold=3, monitored_exceptions=ValueError)
```

```python
from cliffracer_resilience import CircuitBreakerConfig

config = CircuitBreakerConfig(failure_threshold=3, monitored_exceptions=(ValueError,))
```

### The rate limiter's bucket carries the subject prefix

<!-- changelog.d: the-rate-limiter-bucket-carries-the-subject-prefix.md -->

**You see.** With `subject_prefix="px"`, `KvRateLimiter`'s bucket is `px_rate_limits` (stream
`KV_px_rate_limits`) where it was `rate_limits`; with no prefix the name is unchanged. A deployment
that sets a prefix gets a fresh bucket on upgrade, so its limits start from zero, and the old
`rate_limits` bucket keeps its counters until it expires or is deleted. A limiter shared by
services with different prefixes raises `ConfigurationError` when the second one starts, rather
than counting both in one bucket.

**Change.** Nothing, to accept the fresh bucket. The name is on the broker, so anything outside the
service that names `rate_limits`, such as a broker permission or a dashboard, names the new one.

**Your choice.** Whether to start from zero. To keep counting in the existing bucket, open it
yourself and hand it to the limiter: a limiter given a bucket keeps the name it was given. The
old bucket is removed by the operator; it is not removed for you. The naming of every broker
resource is in the table in [decisions.md](decisions.md), and the limiter in the
[resilience README](../packages/cliffracer-resilience/README.md#3-distributed-rate-limiter-with-nats-kv).

```text
from cliffracer_resilience import KvRateLimiter

limiter = KvRateLimiter()
limiter.use_subject_prefix("px")
assert limiter.bucket_wire_name == "rate_limits", "the prefix is not in the bucket name"
```

```python
from cliffracer_resilience import KvRateLimiter

limiter = KvRateLimiter()
limiter.use_subject_prefix("px")
assert limiter.bucket_wire_name == "px_rate_limits"


async def keep_the_existing_counters(js) -> KvRateLimiter:
    bucket = await js.key_value("rate_limits")
    return KvRateLimiter(kv=bucket)
```

### A rate limit that cannot work is refused where it is declared

<!-- changelog.d: a-rate-limit-that-cannot-work-is-refused-where-it-is-declared.md -->

**You see.** `@rate_limit`, `RateLimitConfig` and `ResilienceExtension(default_calls=,
default_window=)` raise `ConfigurationError` where the limit is declared, which for the decorator
is at import. `calls` must be an `int` of at least 1: zero or less, a bool, a float (`2.0` as much
as `2.5`), a string and `None` are refused. `window` must be an `int` or a `float` that is finite
and above 0: zero or less, a bool, a string, `None`, `nan` and infinity are refused. A default
limit takes `default_calls` and `default_window` both or neither, and `None` for either is leaving
it out.

Each was accepted and did something else. A window of zero or less let every call through, `nan`
or infinity refused for ever after the first `calls`, `calls` of zero or less refused every call, a
bool counted as 1 and `calls=2.5` allowed three. A string or `None` raised `TypeError` at the first
call, not where it was declared.

**Change.** Give `calls` a whole number of at least 1 and `window` a finite number of seconds above
0, and give a default limit both.

**Your choice.** The figure. A limit that let every call through never limited, so removing it keeps
what the service does today, and giving it a window starts limiting.

```text
from cliffracer_resilience import rate_limit


@rate_limit(calls=10, window=0)
async def create_order() -> None: ...
```

```python
from cliffracer_resilience import rate_limit


@rate_limit(calls=10, window=60.0)
async def create_order() -> None: ...
```

### A rate limit counts per service and per handler

<!-- changelog.d: a-rate-limit-counts-per-service-and-per-handler.md -->

**You see.** A limit's counter is under `<namespace>.<service>:<handler>:<value>` (without the
namespace when the service has none, and `<service>:<handler>` for a handler that declares no key)
where it was under the key's value alone. The limiter sees that key, and the replicas of one
service still share a counter. Three things follow.

- Counters restart. In a `KvRateLimiter` bucket the entries are under new keys, so every counter
  starts from zero when the service is upgraded, and the old entries stay in the bucket until
  `prune_expired` or the bucket's TTL removes them. While replicas of both versions run, each counts
  under its own keys, and a caller can spend both budgets.
- A budget that was shared is gone. Two handlers keyed on one header count each caller on their
  own, and two services that name a handler alike count in separate entries of a shared bucket.
  A budget shared across handlers or services cannot be declared.
- `reset(key)` takes the counted key, so one given a caller's value alone finds no counter.

A function decorated with `@rate_limit` and called directly, outside a service's handler dispatch,
is not scoped: it counts under the key's value alone, or `global` with no key, as before.

**Change.** Reset with the counted key. A deployment that relied on a shared budget gives each
limit its own figure.

**Your choice.** Whether to sweep the old entries now. `await limiter.prune_expired(window)`
deletes the entries whose timestamps have all left `window`; give it the longest window any
service using the bucket declares. Otherwise they wait for the bucket's TTL, which the limiter sets
only when it creates the bucket. The figures themselves are yours to set.

```text
async def forget(limiter, caller: str) -> None:
    await limiter.reset(caller)
```

```python
async def forget(limiter, caller: str) -> None:
    # a service with a namespace counts under f"{namespace}.catalog:search:{caller}"
    await limiter.reset(f"catalog:search:{caller}")
```

## Metrics

### A latency percentile is a nearest-rank sample

<!-- changelog.d: a-percentile-is-its-nearest-rank-sample.md -->

**You see.** `PerformanceMetrics` reports `p95_ms` and `p99_ms` as nearest-rank percentiles. The
figure read one sample too high: the p95 of 20 samples was the maximum, and the p99 of 100 was the
maximum, so a single slow request set it. The p95 of 1 to 100 ms is 95, not 96.
`check_performance_targets()`, which judges latency on p95, can pass a window it failed.

**Change.** Nothing in code. A dashboard or an alert tuned to the old figure reads lower.

**Your choice.** Whether the threshold moves with it. `targets["max_latency_ms"]` is a plain value
you set.

```text
from cliffracer_metrics import PerformanceMetrics

metrics = PerformanceMetrics()
for ms in [10.0] * 19 + [900.0]:
    metrics.record_latency(ms)
assert metrics.get_latency_stats()["p95_ms"] == 900.0, "one slow request set the p95"
```

```python
from cliffracer_metrics import PerformanceMetrics

metrics = PerformanceMetrics()
for ms in [10.0] * 19 + [900.0]:
    metrics.record_latency(ms)
assert metrics.get_latency_stats()["p95_ms"] == 10.0
assert metrics.get_latency_stats()["max_ms"] == 900.0
```

### `active_connections` goes up and down

<!-- changelog.d: active-connections-can-go-down.md -->

**You see.** `PerformanceMetrics.active_connections` is how many connections are open now. It
moves up on a `connection_opened` event and down on `connection_closed`, and
`set_active_connections(n)` sets it outright. It was the number of times an `active_connections`
event had been recorded, which only went up. Recording that event raises `ValueError` pointing at
the new ones, as does any event name `record_connection_event` does not know, which was ignored
without a word.

**Change.** Record `connection_opened` and `connection_closed`, or set the level.

```text
from cliffracer_metrics import PerformanceMetrics

metrics = PerformanceMetrics()
metrics.record_connection_event("active_connections")
```

```python
from cliffracer_metrics import PerformanceMetrics

metrics = PerformanceMetrics()
metrics.record_connection_event("connection_opened")
metrics.record_connection_event("connection_opened")
metrics.record_connection_event("connection_closed")
assert metrics.get_connection_stats()["active_connections"] == 1
```

### `BatchProcessor.add_item` says what each caller receives

<!-- changelog.d: add-item-says-what-each-caller-receives.md -->

**You see.** What a caller receives depends on `results`, not on the shape of what the processor
returned. A processor that returns one result for the whole batch needs nothing: every caller gets
that value as it is. A processor that returns one result per item is added with
`results="per_item"`, and only then does each caller get its own element. Callers of a per-item
processor that do not pass it each receive the whole returned list. The old rule gave each caller
its own element when the return value was a list as long as the batch, which also unpacked a
processor returning one aggregate list whenever it was as long as the batch, always for a batch
of one. With `"per_item"`, a return value that is not a list or tuple with one result for each
item fails every caller of that call with a `ValueError`. Items added with the same processor and
different `results` are processed in separate calls.

**Change.** Pass `results="per_item"` for a processor that returns one result per item.

```text
import asyncio

from cliffracer_metrics import BatchProcessor


async def score_all(items: list[int]) -> list[int]:
    return [item * 2 for item in items]


async def main() -> None:
    batcher = BatchProcessor(batch_size=2, batch_timeout_ms=20)
    results = await asyncio.gather(
        batcher.add_item("scores", 1, score_all), batcher.add_item("scores", 2, score_all)
    )
    assert results == [2, 4], "each caller receives its own element"
```

```python
import asyncio

from cliffracer_metrics import BatchProcessor


async def score_all(items: list[int]) -> list[int]:
    return [item * 2 for item in items]


async def main() -> None:
    batcher = BatchProcessor(batch_size=2, batch_timeout_ms=20)
    results = await asyncio.gather(
        batcher.add_item("scores", 1, score_all, results="per_item"),
        batcher.add_item("scores", 2, score_all, results="per_item"),
    )
    assert results == [2, 4]
```

## Health

### Readiness asks the broker, not only nats-py's flag

<!-- changelog.d: ready-sends-a-bounded-round-trip-to-the-broker.md -->

**You see.** `/ready` and `/health` send the broker a round trip while nats-py's connection flag
says connected, and report `disconnected` (503, `nats_connected` false) when it is not answered
within `broker_probe_timeout`, 2 seconds by default. Before, a connection that had gone silent
without being reset, as when a partition drops packets, kept answering 200 until nats-py's own ping
loop gave up, 240 to 360 seconds at its defaults. The payload gains `nats_rtt_ms`, the last round
trip in milliseconds, `null` when none was measured. A readiness probe with a failure threshold of 1
can now take a pod out of rotation on one missed round trip.

**Change.** Nothing, if taking a pod out of rotation within about 2 seconds of the broker stalling
is what you want. Check the readiness probe's failure threshold and period in the orchestrator: a
`failureThreshold` above 1 keeps a single missed round trip from acting. `/live` is unchanged and
never asks the broker.

**Your choice.** The bound, `ServiceConfig.broker_probe_timeout`; how long the last answer is
reused, `ServiceConfig.broker_probe_cache` (1 second, a failure included, so a burst of probes costs
one round trip); or `broker_probe_timeout=None`, which turns the round trip off and reads nats-py's
flag alone as before. What can make readiness go down while the broker is fine is listed in the
[architecture guide](ARCHITECTURE.md), and the ruling is in [decisions.md](decisions.md).

```text
import asyncio

from cliffracer import CliffracerService, ServiceConfig


class Silent:
    """A client whose flags say connected and that never answers a PING."""

    is_closed = False
    is_connected = True
    is_draining = False
    is_connecting = False

    def __init__(self):
        self._pongs = []

    async def _send_ping(self, future=None):
        self._pongs.append(future)


service = CliffracerService(ServiceConfig(name="orders", health_port=0))
service._running = True
service.nc = Silent()
health = asyncio.run(service.health_check())
assert health["status"] == "healthy", "readiness reads the flag, not the broker"
```

```python
import asyncio

from cliffracer import CliffracerService, ServiceConfig


class Silent:
    """A client whose flags say connected and that never answers a PING."""

    is_closed = False
    is_connected = True
    is_draining = False
    is_connecting = False

    def __init__(self):
        self._pongs = []

    async def _send_ping(self, future=None):
        self._pongs.append(future)


service = CliffracerService(ServiceConfig(name="orders", health_port=0, broker_probe_timeout=0.1))
service._running = True
service.nc = Silent()
health = asyncio.run(service.health_check())
assert health["status"] == "disconnected" and health["nats_rtt_ms"] is None

# To keep reading nats-py's flag alone, as before:
flag_only = CliffracerService(ServiceConfig(name="orders", health_port=0, broker_probe_timeout=None))
flag_only._running = True
flag_only.nc = Silent()
assert asyncio.run(flag_only.health_check())["status"] == "healthy"
```

## Tracing

### An inbound span is named for the handler, an event is a consumer span, a timer is internal, and `describe` has none

<!-- changelog.d: inbound-spans-are-named-for-the-handler-and-events-are-consumer-spans.md -->

**You see.** With `OtelExtension`, an inbound span is named `{kind} {handler}`: `rpc get_order`,
`async_rpc reindex`, `event on_order`, `timer sweep`. It was named for the subject
(`rpc orders.123.get_order`) and for a timer with the kind alone (`timer`). An `event` span, from core
NATS or JetStream, is `CONSUMER` where it was `SERVER`, and a `timer` span is `INTERNAL` where it was
`SERVER`; `rpc` and `async_rpc` spans stay `SERVER`. A `describe` request starts no span, so neither `spans_total` nor `errors_total` on `/health` counts it, failed or not.
Outbound spans are named as before. The subject is still on every span, in `messaging.destination.name`
and `cliffracer.subject`.

**Change.** Anything that finds a span by its old name has to use the handler's: a saved query, an
alert, a dashboard panel, a sampling rule that matches on the span name, a test that looks a span up.
Filter on `messaging.destination.name` where you meant one subject. A view of server-side requests
or latency does not include event handlers or timers; a view of consumed messages does, and a timer
is an internal span. A count that
included `describe` spans is lower by those requests, and a `describe` that fails is not in `errors_total`.

**Your choice.** Whether a query that grouped by subject should now group by handler (one line per
handler) or filter on `messaging.destination.name` (one line per subject, as before). The package
README, [cliffracer-otel](../packages/cliffracer-otel/README.md), lists the attribute names a span carries.

```text
from opentelemetry.trace import SpanKind


def check(spans):
    """`spans`: what your exporter holds after the service handled an RPC, an event, a timer tick and a describe."""
    assert any(s.name == "rpc orders.123.get_order" for s in spans), "spans are named for the handler now"
    assert all(s.kind == SpanKind.SERVER for s in spans)
```

```python
from opentelemetry.trace import SpanKind


def check(spans):
    """`spans`: what your exporter holds after the service handled an RPC, an event, a timer tick and a describe."""
    by_name = {s.name: s for s in spans}
    rpc_span = by_name["rpc get_order"]
    assert rpc_span.kind == SpanKind.SERVER
    assert rpc_span.attributes["messaging.destination.name"] == "orders.123.get_order"
    assert by_name["event on_order"].kind == SpanKind.CONSUMER
    assert by_name["timer sweep"].kind == SpanKind.INTERNAL
    assert not [s for s in spans if s.name.startswith("describe")]
```

## Cron

### A distributed cron job's lease fits its bucket's TTL

<!-- changelog.d: a-cron-job-whose-lease-outlives-its-bucket-is-refused.md -->

**You see.** `start()` raises `ConfigurationError` for a distributed `@cron` job with `no_overlap`
(the default) whose `lease_ttl` is longer than the TTL of the bucket it is handed. The error names
the job, its `lease_ttl`, the bucket and the bucket's TTL. A bucket expires every key in it, active
leases included, after a TTL that is fixed by whichever job opened it first, so a job that asked for
a one-hour lease on a bucket another job had opened with five minutes had its overlap lease vanish
after five, silently. The default `lease_ttl` is 300 seconds, so a job on a bucket created with a
shorter TTL (60 seconds, say) is refused, where it started and lost its lease early. A job with
`no_overlap=False` writes no lease and is not checked. A bucket that keeps keys longer than the job
asked, or has no expiry, is used as it is, and a bucket whose TTL cannot be read at start is logged
and does not stop the job.

**Change.** Give the job a bucket of its own with `bucket=`; the timer creates it with a TTL of at
least the job's `lease_ttl`. Or start the job with the longest `lease_ttl` first, so that it opens
the shared bucket.

**Your choice.** Whether the job may overlap itself. `no_overlap=False` clears the refusal, and with
it there is no lease: a run that is still going when the next interval fires is not waited for.

```text
from cliffracer import CliffracerService
from cliffracer_cron import cron
from cliffracer_kv import KvExtension


class Reports(CliffracerService):
    kv = KvExtension()

    # The shared `cron_locks` bucket was opened with a 300 second TTL by another job.
    @cron("0 9 * * *", distributed=True, lease_ttl=3600)
    async def nightly(self) -> None: ...
```

```python
from cliffracer import CliffracerService
from cliffracer_cron import cron
from cliffracer_kv import KvExtension


class Reports(CliffracerService):
    kv = KvExtension()

    @cron("0 9 * * *", distributed=True, lease_ttl=3600, bucket="reports_locks")
    async def nightly(self) -> None: ...
```

### A distributed cron job's options are checked where it is declared

<!-- changelog.d: a-distributed-cron-job-refuses-an-option-it-cannot-run-with.md -->

**You see.** `@cron(..., distributed=True)` and `DistributedCronTimer(...)` raise
`ConfigurationError` where the job is declared, which is at import, for a `lease_ttl` that is not a
finite number of seconds above zero (0, negative, `nan`, `inf`, a bool, a string), for a
`no_overlap` that is not a bool, and for a `bucket` the Key-Value layer would refuse (an empty
name, `a.b`, a name with a space). A `lease_ttl` of 0 or less made `no_overlap` skip nothing, `nan`
or a string failed at every firing, and a bad bucket name failed at the first firing.

**Change.** Give each option a value it can hold: a `lease_ttl` of a finite number of seconds above
zero, `no_overlap` as `True` or `False`, and a bucket name of letters, digits, `_` and `-`.

**Your choice.** The lease. A `lease_ttl` of 0 asked for no overlap protection at all; how long a
run may take before another replica starts over it is yours to name.

```text
from cliffracer import CliffracerService
from cliffracer_cron import cron
from cliffracer_kv import KvExtension


class Reports(CliffracerService):
    kv = KvExtension()

    @cron("0 9 * * *", distributed=True, lease_ttl=0)
    async def nightly(self) -> None: ...
```

```python
from cliffracer import CliffracerService
from cliffracer_cron import cron
from cliffracer_kv import KvExtension


class Reports(CliffracerService):
    kv = KvExtension()

    @cron("0 9 * * *", distributed=True, lease_ttl=600)
    async def nightly(self) -> None: ...
```

### A distributed cron job's lock keys carry the service's namespace

<!-- changelog.d: cron-lock-keys-carry-the-namespace.md -->

**You see.** A service with a `namespace` writes its cron lock keys as
`cron.<namespace>.<service>.<method>.<epoch>`, and the same stem for the overlap lease (`.active`)
and the eager lock (`.eager`), in the same `cron_locks` bucket, where it wrote
`cron.<service>.<method>.<epoch>`. Two apps on one broker with the same service name and schedule
shared one key per firing, so one app's job never ran; each now runs its own. A service with
no namespace has the keys it had.

Replicas of the old and the new version arbitrate on different keys, so during a rolling upgrade of
a service with a namespace, a firing that falls while both are up runs once on each version.
Anything that reads or deletes a lock by key, a runbook that clears a stuck lease say, names the old
key and finds nothing. The keys are not escaped: a service named `a.b` with no namespace and a
service named `b` in the namespace `a` share their keys, and only one of them runs a firing.

**Change.** Stop every old replica before the first new one starts, a recreate and not a rolling
update, or begin the upgrade so that no scheduled time falls while both versions are up. Change what
names a lock key to the new form.

**Your choice.** How the replicas are replaced. Stopping first leaves a gap in which nothing serves
the service's other work; a rolling update keeps serving and needs a window between two firings.

```text
async def clear_stuck_lease(bucket) -> None:
    await bucket.delete("cron.billing.settle.active")
```

```python
async def clear_stuck_lease(bucket) -> None:
    await bucket.delete("cron.app1.billing.settle.active")
```

## Testing

### `MockMessage.metadata` raises without metadata, and the mock sequence is a `SequencePair`

<!-- changelog.d: mock-message-metadata-raises-when-absent-and-the-mock-sequence-is-the-real-pair.md -->

**You see.** In a test that uses `cliffracer.testing`, `MockMessage.metadata` raises
`NotJSMessageError` when the message was given no metadata, as the property on a core `nats` message
does, where it answered `None`. `MockJetStreamMetadata.sequence` is a `SequencePair(consumer,
stream)` like a real delivery's, where it was an `int`, so a test that compared it with a number
compares `sequence.stream` instead. A dead letter made from a delivery the harness builds
(`ServiceTestHarness.deliver_jetstream`) carries `stream_sequence` and a `Nats-Msg-Id` of the form
`dlq:<service>:<stream>:<stream_sequence>:<consumer>`. A dead letter reads the stream sequence from
`sequence.stream`, so a `sequence=` given a number records neither.

**Change.** Catch `NotJSMessageError` where a test reads `metadata` from a message it built with
none, or give the message the `MockJetStreamMetadata` it means. Pass `sequence=` a `SequencePair`.

```text
from cliffracer.testing import MockJetStreamMetadata, MockMessage


def has_metadata(msg: MockMessage) -> bool:
    return msg.metadata is not None


def a_delivery() -> MockMessage:
    return MockMessage("orders.created", metadata=MockJetStreamMetadata(sequence=7))
```

```python
from nats.aio.msg import Msg
from nats.errors import NotJSMessageError

from cliffracer.testing import MockJetStreamMetadata, MockMessage


def has_metadata(msg: MockMessage) -> bool:
    try:
        msg.metadata
    except NotJSMessageError:
        return False
    return True


def a_delivery() -> MockMessage:
    pair = Msg.Metadata.SequencePair(consumer=7, stream=7)
    return MockMessage("orders.created", metadata=MockJetStreamMetadata(sequence=pair))
```

## Packages

### pydantic 2.11 or later is required

<!-- changelog.d: the-pydantic-floor-is-a-version-the-library-works-on.md -->

**You see.** An installer refuses to resolve a project that pins pydantic below 2.11 next to this
release: `cliffracer` and `cliffracer-kv` require `pydantic>=2.11.0,<3.0.0`. The 1.x releases
declared `pydantic>=2.0.0` and installed on older versions, where they did not work as documented.
On 2.5 `normalize()` of any service template raised `AttributeError: 'FieldInfo' object has no
attribute 'init'`. On 2.6 to 2.10 the template settings checks, the output bindings and the
handling of models that declare `validate_by_alias` or `validate_by_name` (configuration that
pydantic added in 2.11) behaved differently from 2.11, which is the version the suite runs on.

**Change.** Raise the pin, or the constraint in a lock or constraints file, to 2.11 or later, or
remove it. Nothing else is needed.

```text
from packaging.specifiers import SpecifierSet

required = SpecifierSet(">=2.11.0,<3.0.0")  # what cliffracer and cliffracer-kv declare
pinned = "2.10.6"  # constraints.txt: pydantic==2.10.6

assert pinned in required, f"pydantic {pinned} does not satisfy {required}"
```

```python
from packaging.specifiers import SpecifierSet

required = SpecifierSet(">=2.11.0,<3.0.0")  # what cliffracer and cliffracer-kv declare
pinned = "2.11.7"  # constraints.txt: pydantic==2.11.7

assert pinned in required, f"pydantic {pinned} does not satisfy {required}"
```

### The `cliffracer-faststream`, `cliffracer-backdoor` and `cliffracer-http` distributions are gone

**You see.** `FastStreamExtension`, `BackdoorExtension`, `HttpExtension`, `AutoGatewayExtension` and the
route and websocket decorators do not exist, and the three distributions that held them are not part
of this release. A service that imports any of them fails to start with an `ImportError`. A service that
had one of them declared also stops publishing to it: `CliffracerService.broadcast_message` publishes
the event and does not call `broadcast_to_websockets` on an extension that defines it.

**Change.** Remove the imports and the extension declarations. A front end that took HTTP, WebSocket or
stream traffic in the service's own process is now a process of its own that calls the service over the
broker with `ServiceClient` (or the generated client for it); the service keeps its `@rpc` and
`@listener` handlers as they are.

**Your choice.** What the front end is. This page decides nothing: the library ships none.

```text
from cliffracer_http import HttpExtension

class Gateway(CliffracerService):
    http = HttpExtension()
```

```python
from cliffracer import CliffracerService, ServiceConfig, rpc
from cliffracer.client import ServiceClient


class Orders(CliffracerService):
    def __init__(self):
        super().__init__(ServiceConfig(name="orders"))

    @rpc
    async def get_order(self, order_id: str) -> dict[str, str]:
        return {"order_id": order_id}


# The front end is its own process, and reaches the service through the broker:
client = ServiceClient(service="orders", nats_url="nats://localhost:4222")
```
