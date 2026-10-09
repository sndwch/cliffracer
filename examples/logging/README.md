# Log streaming

A service writes its logs to stderr and, with `LoggingExtension(to_nats=True)`,
also publishes them to NATS. A separate ingester subscribes to those subjects
and forwards them to OpenObserve.

```
Service (loguru) ──> NATS ──> log ingester ──> OpenObserve (storage on MinIO)
       │
       └──> stderr ──> docker logs, /var/log
```

## Quick start

### 1. Start the infrastructure

```bash
cd examples/logging
docker compose up -d
```

`docker-compose.yml` in this directory brings up NATS, MinIO and OpenObserve.

### 2. Stream a service's logs to NATS

```python
from cliffracer import CliffracerService, ServiceConfig
from cliffracer_logging import LoggingExtension

class MyService(CliffracerService):
    logging = LoggingExtension(to_nats=True)

service = MyService(ServiceConfig(
    name="my_service",
    nats_url="nats://localhost:4222",
    log_level="INFO",
))

service.run()
```

The extension attaches its sink in `start()` and removes it in `stop()`, so a
service that restarts in-process has one sink.

Log streaming is the extension's setting. `ServiceConfig` refuses `log_to_nats`
by name, so writing it there raises a `ValidationError` naming the field.

`health_details()` reports `{"to_nats": bool, "streaming": bool}`, where
`streaming` is whether the sink is attached.

### 3. Start the log ingester

```bash
cd examples/logging
python log_ingester.py
```

Or through Docker:

```bash
docker-compose up log_ingester
```

The ingester listens on `logs.>` with `fanout=True`. It logs to stderr only,
which keeps its own log lines out of the subjects it reads.

### 4. View the logs

Open http://localhost:5080, sign in as `admin@example.com` with password
`password`, and go to **Streams** → **cliffracer_logs**.

## Log subjects

Logs are published to a subject built from the service name and the level, and
scoped like every other wire subject:

```
<prefix>.<namespace>.logs.<service_name>.<level>
```

The prefix and namespace are only present when the service sets them. With
neither, the subject is `logs.user_service.info`; under `namespace="appA"` it is
`appA.logs.user_service.info`.

**The sink and the ingester derive this from the same builder**, so they cannot
disagree: a namespaced service's logs go to that namespace's ingester. Watching
every namespace is a deliberate choice rather than the default -- ask for it
with the wildcard, in a `cross_namespace=True` listener or in the subject below.

| subscription | what it gets |
|---|---|
| `logs.>` | every service, **only where no namespace is set** |
| `appA.logs.>` | every service in `appA` |
| `appA.logs.user_service.>` | one service in `appA` |
| `appA.logs.*.error` | errors from all services in `appA` |
| `*.logs.>` | every namespace, where no `subject_prefix` is set |
| `*.*.logs.>` | every namespace under any prefix |

**A `*` spans exactly one token, so there is no one pattern that covers every
deployment** -- add one `*.` for each scoping token your services set. With
neither a prefix nor a namespace that is `logs.>`; with a namespace, `*.logs.>`;
with both, `*.*.logs.>`. A pattern with too few tokens matches nothing and says
nothing about it, which is the same silence this whole section exists because of.

Read them from the command line. These assume `namespace="appA"` and no prefix:

```bash
nats sub "appA.logs.>"
nats sub "appA.logs.*.error"
nats sub "*.logs.>" --translate "jq ."
```

## Ingester environment variables

Each is shown with its default:

```bash
NATS_URL=nats://localhost:4222

OPENOBSERVE_URL=http://localhost:5080
OPENOBSERVE_ORG=default
OPENOBSERVE_STREAM=cliffracer_logs
OPENOBSERVE_USER=admin@example.com
OPENOBSERVE_PASSWORD=password

BATCH_SIZE=100          # flush after N logs
FLUSH_INTERVAL=5        # flush every N seconds
```

Lower them to flush sooner and hold a smaller buffer:

```bash
FLUSH_INTERVAL=1 BATCH_SIZE=50 python log_ingester.py
```

## Querying in OpenObserve

```sql
-- All errors in the last hour
SELECT * FROM cliffracer_logs
WHERE record.level.name = 'ERROR'
AND _timestamp > now() - interval '1 hour'

-- Logs from one service
SELECT * FROM cliffracer_logs
WHERE extra.service = 'user_service'
ORDER BY _timestamp DESC
LIMIT 100
```

## Adding context to a log line

```python
from loguru import logger

logger.bind(
    user_id="12345",
    request_id="abc-def",
    correlation_id="xyz"
).info("Processing payment")
```

Bound values arrive in OpenObserve under `extra`, as long as they can be pickled: see below.

Structured JSON is the default: `LoggingConfig.configure(service_name)` takes
`structured=True`.

Every sink `LoggingConfig` adds uses loguru's `enqueue=True`, which hands the
record to a queue rather than formatting it on the calling task.

The queue pickles each record, so a record that carries a value which cannot be pickled,
such as a live connection, a lock, a lambda or a model holding one, is dropped by that sink.
Loguru prints a `Logging error in Loguru Handler` traceback to stderr for it, and every other
line keeps flowing. Bind plain values (ids, names, counts), or the value's `repr`, rather than
the object.

## Applying a domain-specific NATS redactor

```python
from cliffracer_logging import LoggingExtension

def redact_customer_data(record):
    record["record"]["extra"].pop("customer_email", None)
    return record

logging = LoggingExtension(to_nats=True, redactor=redact_customer_data)
```

Common credential fields are redacted recursively by default. A custom
redactor replaces that policy and can remove domain-specific values before a
record reaches NATS. Do not interpolate credentials into free-form messages.

## Examples in this directory

- `extension_example.py` — a service declaring `LoggingExtension`
- `log_ingester.py` — the `logs.>` subscriber that forwards to OpenObserve
  (resolved to `<namespace>.logs.>` for a namespaced deployment)

## Troubleshooting

Logs not arriving in OpenObserve — work along the path:

```bash
nats server ping                      # the broker is up
nats sub "*.logs.>"                   # the service is publishing (see Log subjects:
                                      #   logs.> with no namespace, *.*.logs.> under a prefix)
docker logs log_ingester              # the ingester is running
curl http://localhost:5080/healthz    # OpenObserve is up
```

## Why NATS sits in the middle

Sending to NATS and letting an ingester forward keeps the two independent. The
services publish to a subject; anything that wants the logs subscribes to it.
That buys buffering while OpenObserve is down, room for more than one consumer,
and a publish that returns without waiting for the log store.

To use a different backend — Loki, Quickwit, Elasticsearch, or NATS JetStream's
own retention — change the ingester's `_flush_to_*` method.
