# My Business App

Example application built with the Cliffracer microservices framework.

## Features

- **Product Management**: Create, list, and retrieve products
- **HTTP API**: REST endpoints with automatic OpenAPI documentation
- **Event-Driven**: Publishes events when products are created
- **Database Integration**: PostgreSQL with secure repository pattern
- **Health Monitoring**: Built-in health checks and service discovery
- **Correlation Tracking**: Request tracing across services

## Installation

```bash
# Install dependencies (includes Cliffracer)
pip install -e .

# Or with uv
uv sync
```

## Running

### Prerequisites

```bash
# Start NATS server
docker run -d --name nats -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222

# Start PostgreSQL (optional, for database features)
docker run -d --name postgres -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:15
```

### Start the Service

```bash
python my_service.py
```

The service will start:
- **NATS services** on `nats://localhost:4222`
- **HTTP API** on `http://localhost:8080`
- **API docs** at `http://localhost:8080/docs`

## API Usage

### Create a Product

```bash
curl -X POST http://localhost:8080/products \
  -H "Content-Type: application/json" \
  -d '{"name": "Widget", "price": 29.99, "category": "gadgets"}'
```

### List Products

```bash
curl http://localhost:8080/products
```

### Get Product by ID

```bash
curl http://localhost:8080/products/{product_id}
```

### Health Check

```bash
curl http://localhost:8080/health
```

## RPC Usage

You can also call services via NATS RPC:

```python
from cliffracer import CliffracerService

class ClientService(CliffracerService):
    def __init__(self):
        super().__init__(name="client_service")
    
    async def test_calls(self):
        # Call the business service via RPC
        result = await self.call_rpc(
            "my_business_service", 
            "create_product",
            name="RPC Widget",
            price=39.99,
            category="tools"
        )
        print(f"Created product: {result}")
        
        # List products
        products = await self.call_rpc("my_business_service", "list_products")
        print(f"Found {products['count']} products")

client = ClientService()
client.run()
```

## Event Handling

Services automatically handle events:

```python
@self.event("product.created")
async def on_product_created(self, product_data: dict):
    print(f"New product created: {product_data['name']}")
    # Send email notifications, update analytics, etc.
```

## Configuration

Set environment variables:

```bash
# NATS Configuration
NATS_URL=nats://localhost:4222

# Database Configuration
DB_HOST=localhost
DB_PORT=5432
DB_USER=myapp
DB_PASSWORD=myapp
DB_NAME=myapp

# Authentication
AUTH_SECRET_KEY=your-secret-key-here
```

## Architecture

This example demonstrates:

1. **Service Layer**: Business logic in Cliffracer services
2. **HTTP Layer**: REST API with FastAPI integration
3. **Data Layer**: PostgreSQL with secure repository pattern
4. **Messaging Layer**: NATS for inter-service communication
5. **Event Layer**: Publish/subscribe for loose coupling

## Production Deployment

See [../INSTALL.md](../INSTALL.md) for Docker and Kubernetes deployment examples.

---

**Built with Cliffracer - Production-ready microservices made simple! 🚀**