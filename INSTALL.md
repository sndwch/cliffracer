# Cliffracer Installation Guide

This guide shows how to install and use Cliffracer in your projects.

## 🚀 Quick Install

### Option 1: Install from Built Package (Recommended)

```bash
# Build the package (run from Cliffracer directory)
uv build

# Install in your project
pip install /path/to/cliffracer/dist/cliffracer-1.0.0-py3-none-any.whl

# Or with uv
uv add /path/to/cliffracer/dist/cliffracer-1.0.0-py3-none-any.whl
```

### Option 2: Development Install (Editable)

```bash
# From your project directory
pip install -e /path/to/cliffracer

# Or with uv
uv add --editable /path/to/cliffracer
```

### Option 3: pyproject.toml Dependency

Add to your project's `pyproject.toml`:

```toml
[project]
dependencies = [
    "cliffracer @ file:///absolute/path/to/cliffracer",
    # or relative path:
    # "cliffracer @ file://../cliffracer",
]

# Optional: include specific features
[project.optional-dependencies]
web = ["cliffracer[extended]"]  # HTTP/WebSocket support
aws = ["cliffracer[aws]"]       # AWS messaging backend
monitoring = ["cliffracer[monitoring]"]  # Metrics and monitoring
all = ["cliffracer[all]"]       # Everything
```

Then:
```bash
uv sync  # Installs all dependencies including Cliffracer
```

## 📦 Package Features

### Core Installation (Minimal)
```bash
# Just the essentials: NATS, database, auth
pip install cliffracer
```

Includes:
- NATS messaging and RPC
- PostgreSQL database integration
- JWT authentication
- Correlation tracking
- Input validation

### Extended Installation (Recommended)
```bash
# Adds HTTP/WebSocket support
pip install cliffracer[extended]
```

Additional features:
- FastAPI HTTP endpoints
- WebSocket real-time communication
- CORS support
- Automatic OpenAPI docs

### Full Installation (Everything)
```bash
# All features enabled
pip install cliffracer[all]
```

Includes everything plus:
- AWS messaging backends
- Monitoring and metrics
- Development tools
- Documentation tools

## 🔧 Usage in Your Project

### Basic Service Example

```python
# your_service.py
from cliffracer import CliffracerService

class MyService(CliffracerService):
    def __init__(self):
        super().__init__(
            name="my_service",
            nats_url="nats://localhost:4222"
        )

    @self.rpc
    async def hello(self, name: str) -> str:
        return f"Hello, {name}!"

    @self.event("user.created")
    async def on_user_created(self, user_data: dict):
        print(f"New user: {user_data}")

if __name__ == "__main__":
    service = MyService()
    service.run()
```

### HTTP Service Example

```python
from cliffracer import CliffracerService
from cliffracer.core.mixins import HTTPMixin

class APIService(CliffracerService, HTTPMixin):
    def __init__(self):
        super().__init__(
            name="api_service",
            nats_url="nats://localhost:4222",
            http_port=8080
        )

    @self.rpc
    @self.http.get("/users/{user_id}")
    async def get_user(self, user_id: str) -> dict:
        return {"user_id": user_id, "name": "John Doe"}

service = APIService()
service.run()
```

### Database Integration Example

```python
from cliffracer import CliffracerService
from cliffracer.database import SecureRepository
from cliffracer.database.models import DatabaseModel
from pydantic import Field

class User(DatabaseModel):
    __tablename__ = "users"
    
    name: str = Field(..., description="User name")
    email: str = Field(..., description="User email")

class UserService(CliffracerService):
    def __init__(self):
        super().__init__(name="user_service")
        self.users = SecureRepository(User)

    @self.rpc
    async def create_user(self, name: str, email: str) -> dict:
        user = User(name=name, email=email)
        created = await self.users.create(user)
        return created.model_dump()

service = UserService()
service.run()
```

## ⚙️ Configuration

### Environment Variables

```bash
# NATS Configuration
NATS_URL=nats://localhost:4222
NATS_USER=optional_user
NATS_PASSWORD=optional_password

# Database (optional)
DB_HOST=localhost
DB_PORT=5432
DB_USER=myapp_user
DB_PASSWORD=myapp_password
DB_NAME=myapp

# Authentication (optional)
AUTH_SECRET_KEY=your-super-secret-key-here
AUTH_TOKEN_EXPIRY_HOURS=24

# Debug (optional)
BACKDOOR_ENABLED=false
BACKDOOR_PASSWORD=debug-password
```

### ServiceConfig Example

```python
from cliffracer import ServiceConfig, CliffracerService

config = ServiceConfig(
    name="production_service",
    nats_url="nats://production-nats:4222",
    
    # Database
    db_host="production-postgres",
    db_name="myapp",
    
    # Security
    enable_auth=True,
    auth_secret_key="production-secret-key",
    
    # Performance
    connection_pool_size=20,
    batch_size=500,
    enable_metrics=True
)

service = CliffracerService(config)
```

## 🧪 Verify Installation

Test that everything works:

```python
# test_install.py
from cliffracer import CliffracerService, __version__
from cliffracer.auth.simple_auth import SimpleAuthService, AuthConfig
from cliffracer.database import SecureRepository
from cliffracer.core.correlation import CorrelationContext

print(f"Cliffracer version: {__version__}")

# Test auth
config = AuthConfig(secret_key="test-key-" + "x" * 32)
auth = SimpleAuthService(config)
print("✅ Auth system working")

# Test correlation
corr_id = CorrelationContext.get_or_create_id()
print(f"✅ Correlation ID: {corr_id}")

print("🎉 Cliffracer installed successfully!")
```

```bash
python test_install.py
```

## 🚀 Production Setup

### Dockerfile Example

```dockerfile
FROM python:3.11-slim

WORKDIR /app

# Copy requirements
COPY pyproject.toml uv.lock ./

# Install dependencies
RUN pip install uv && uv sync --no-dev

# Copy application
COPY . .

# Health check
HEALTHCHECK --interval=30s --timeout=10s --start-period=5s \
  CMD curl -f http://localhost:8080/health || exit 1

# Run service
CMD ["python", "-m", "your_service"]
```

### Docker Compose Example

```yaml
version: '3.8'
services:
  nats:
    image: nats:alpine
    ports:
      - "4222:4222"
      - "8222:8222"
    command: ["-js", "-m", "8222"]

  postgres:
    image: postgres:15
    environment:
      POSTGRES_DB: myapp
      POSTGRES_USER: myapp_user
      POSTGRES_PASSWORD: myapp_password
    ports:
      - "5432:5432"

  my-service:
    build: .
    environment:
      NATS_URL: nats://nats:4222
      DB_HOST: postgres
      DB_NAME: myapp
      DB_USER: myapp_user
      DB_PASSWORD: myapp_password
    depends_on:
      - nats
      - postgres
    ports:
      - "8080:8080"
```

## 🔧 Troubleshooting

### Common Issues

**Import Error: No module named 'cliffracer'**
```bash
# Make sure you installed it
pip list | grep cliffracer

# Reinstall if needed
pip install --force-reinstall cliffracer
```

**NATS Connection Error**
```bash
# Check NATS is running
docker run -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222

# Test connection
curl http://localhost:8222/varz
```

**Database Connection Error**
```bash
# Check PostgreSQL is running
docker run -e POSTGRES_PASSWORD=test -p 5432:5432 postgres:15

# Test connection
psql -h localhost -U postgres -d postgres
```

**Auth/JWT Errors**
```python
# Make sure secret key is long enough
from cliffracer.auth.simple_auth import AuthConfig
config = AuthConfig(secret_key="x" * 32)  # Minimum 32 characters
```

### Development Dependencies

For development/testing:

```bash
# Install with dev dependencies
pip install cliffracer[dev]

# Or add to pyproject.toml
[project.optional-dependencies]
dev = ["cliffracer[dev]"]
```

## 📞 Support

- 📖 Documentation: [README.md](README.md)
- 🏗️ Architecture: [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md)
- 🐛 Issues: [GitHub Issues](https://github.com/sndwch/microservices/issues)
- 💬 Examples: [examples/](examples/)

---

**Happy coding with Cliffracer! 🚀**