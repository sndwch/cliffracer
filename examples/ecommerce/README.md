# E-commerce example

Five services on one NATS broker, with an HTTP API on the order service and a
load generator that keeps traffic flowing.

| service | what it does |
|---|---|
| Order | HTTP API and NATS handlers for creating and reading orders |
| Inventory | Reserves stock for an order |
| Payment | Processes payment, simulating a 90% success rate |
| Notification | Sends order notifications |
| Load generator | Creates an order every 2 to 10 seconds |

The order workflow runs create → inventory check → payment → notification. Each
step announces itself with a `@broadcast` handler on a subject the next service
listens to — `orders.order_created`, `inventory.reserved`,
`orders.order_status_changed`. Request and response bodies are Pydantic models,
and a correlation id travels the whole chain.

## Running it

Start a broker:

```bash
docker run -d --name nats-server -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222
```

Then run the system:

```bash
uv sync --all-packages --extra dev
cd examples/ecommerce
python main.py
```

`main.py` starts all five services under a `ServiceOrchestrator` with
`auto_restart=True`. Press Ctrl+C to stop them.

`demo_simple.py` runs the same concepts in one process and needs no broker.

## What to look at

- Order service API and its generated docs: http://localhost:8001/docs
- NATS monitoring: http://localhost:8222
- The terminal, for structured JSON log lines carrying the correlation id

Log lines to follow through a single order:

```
order_created  ->  inventory_reserved  ->  payment_success | payment_failed  ->  notification_sent
```

## Driving it by hand

`test_system.py` creates orders over HTTP and reads them back while the system
runs:

```bash
uv run python test_system.py
```

Create an order directly:

```bash
curl -X POST "http://localhost:8001/orders" \
  -H "Content-Type: application/json" \
  -d '{
    "user_id": "demo_user",
    "items": [
      {
        "product_id": "laptop-pro",
        "name": "Professional Laptop",
        "quantity": 1,
        "price": 1299.99
      }
    ],
    "shipping_address": "123 Demo St, Demo City",
    "email": "demo@example.com"
  }'
```

Read orders back:

```bash
curl http://localhost:8001/orders
curl http://localhost:8001/orders/{order_id}
```

## What the simulation does

The services stand in for real ones with fixed delays and rates, so the example
behaves the same way on any machine:

| | |
|---|---|
| Payment success | 90% |
| Payment delay | 100–500 ms |
| Notification delay | 50–200 ms |
| Order arrival | every 2–10 seconds |
| Items per order | 1–3 |
| Storage | in memory |

## Troubleshooting

Check the broker is up and reachable:

```bash
curl http://localhost:8222/varz
```

When a service reports a connection error, confirm port 4222 is open to it and
read that error in the service log.
