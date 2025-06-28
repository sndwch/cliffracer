# Quick Start

**⚠️ IMPORTANT: Read [../../IMPLEMENTATION_STATUS.md](../../IMPLEMENTATION_STATUS.md) first to understand what actually works.**

Get up and running with the working parts of Cliffracer.

## Prerequisites

Make sure you have completed the [installation](installation.md) and have:

- Python 3.13+ environment 
- NATS server running
- Framework dependencies installed with `uv sync`

## Your First Working Service

Let's build a simple service using only the features that actually work.

### 1. Start NATS Server

```bash
# Start NATS server with Docker
docker run -d --name nats-server -p 4222:4222 nats:alpine
```

### 2. Create a Basic Service

Create `simple_service.py`:

```python
from cliffracer import NATSService, HTTPNATSService, ServiceConfig
from pydantic import BaseModel

class UserRequest(BaseModel):
    username: str
    email: str

class SimpleService(HTTPNATSService):
    def __init__(self):
        config = ServiceConfig(name="simple_service")
        super().__init__(config, port=8001)
        
        # Add working HTTP endpoint
        @self.app.post("/users")
        async def create_user(request: UserRequest):
            # This actually works
            return {"user_id": f"user_{request.username}", "status": "created"}
        
        @self.app.get("/health")
        async def health():
            return {"status": "healthy", "service": "simple_service"}

if __name__ == "__main__":
    service = SimpleService()
    service.run()
```

### 3. Run the Service

```bash
uv run python simple_service.py
```

### 4. Test It

```bash
# Test the health endpoint
curl http://localhost:8001/health

# Test the user creation
curl -X POST "http://localhost:8001/users" \
  -H "Content-Type: application/json" \
  -d '{"username": "test", "email": "test@example.com"}'
```

## What Works vs. What Doesn't

### ✅ What You Can Build Today

- **HTTP APIs** backed by NATS messaging
- **WebSocket services** for real-time communication
- **Service orchestration** with multiple services
- **Schema validation** using Pydantic
- **Structured logging** 

### ❌ What to Avoid (Broken)

- Authentication decorators (`@require_auth` - import errors)
- AWS backend switching (not implemented)
- Monitoring integrations (only file export works)
- Complex broadcast patterns (some examples are broken)

## Next Steps

1. **Try the [E-commerce Example](../../examples/ecommerce/README.md)** - A complete working system
2. **Read [../../IMPLEMENTATION_STATUS.md](../../IMPLEMENTATION_STATUS.md)** for detailed feature status
3. **Check [Load Testing](../../load-testing/README.md)** to validate performance
4. **Focus on NATS core features** which are production-ready

## Need Help?

- **Working Examples**: See `examples/ecommerce/` for a complete functional system
- **Implementation Status**: Check `IMPLEMENTATION_STATUS.md` for current limitations  
- **Debugging**: Use the [backdoor debugging system](../debugging/README.md) that actually works