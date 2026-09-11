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
cd deployment/docker
docker-compose --profile logging up -d
```

That profile brings up NATS, MinIO and OpenObserve.

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

Logs are published to a subject built from the service name and the level:

```
logs.<service_name>.<level>
```

For example `logs.user_service.info`, `logs.order_service.error`.

| subscription | what it gets |
|---|---|
| `logs.>` | every log line from every service |
| `logs.user_service.>` | one service |
| `logs.*.error` | errors from all services |

Read them from the command line:

```bash
nats sub "logs.>"
nats sub "logs.*.error"
nats sub "logs.>" --translate "jq ."
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

Bound values arrive in OpenObserve under `extra`.

Structured JSON is the default: `LoggingConfig.configure(service_name)` takes
`structured=True`.

Every sink `LoggingConfig` adds uses loguru's `enqueue=True`, which hands the
record to a queue rather than formatting it on the calling task.

## Filtering a sink

```python
from loguru import logger

def sanitize_logs(record):
    if "password" in str(record["message"]):
        record["message"] = "[REDACTED]"
    return True

logger.add(sink, filter=sanitize_logs)
```

## Examples in this directory

- `extension_example.py` — a service declaring `LoggingExtension`
- `log_ingester.py` — the `logs.>` subscriber that forwards to OpenObserve

## Troubleshooting

Logs not arriving in OpenObserve — work along the path:

```bash
nats server ping                      # the broker is up
nats sub "logs.>"                     # the service is publishing
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
