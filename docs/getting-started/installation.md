# Installation

This guide will help you install and set up Cliffracer.

## Prerequisites

### System Requirements

- **Python**: 3.11 or higher
- **Docker**: 20.10+ (for containerized deployment)
- **Docker Compose**: 2.0+ (for multi-service setup)
- **NATS Server**: 2.9+ (can be run via Docker)

### Development Environment

We recommend using `uv` for Python version management and dependency installation:

```bash
# Install uv (if not already installed)
curl -LsSf https://astral.sh/uv/install.sh | sh

# The project includes a .python-version file specifying Python 3.13.2
# uv will automatically use this version
```

## Installation Methods

### Method 1: Local Development

#### 1. Clone the Repository

```bash
git clone https://github.com/sndwch/cliffracer.git
cd cliffracer
```

#### 2. Install Dependencies

```bash
# Install all dependencies with uv (includes virtual environment creation)
uv sync --extra dev --extra monitoring

# Alternative: Install different dependency groups
uv sync --extra extended          # Basic HTTP/WebSocket support
uv sync --extra aws              # AWS messaging backend
uv sync --extra all              # All features
```

#### 3. Set Up NATS Server

=== "Docker (Recommended)"

    ```bash
    # Start NATS with JetStream
    docker run -d --name nats-server \
      -p 4222:4222 \
      -p 8222:8222 \
      nats:2.10-alpine \
      -js -m 8222
    ```

=== "Local Installation"

    ```bash
    # Download and install NATS server
    wget https://github.com/nats-io/nats-server/releases/download/v2.10.0/nats-server-v2.10.0-linux-amd64.tar.gz
    tar -xzf nats-server-v2.10.0-linux-amd64.tar.gz
    
    # Run NATS server
    ./nats-server-v2.10.0-linux-amd64/nats-server -js -m 8222
    ```

#### 4. Verify Installation

```bash
# Test basic service
python -c "
from cliffracer import ServiceConfig
config = ServiceConfig(name='test')
print('✅ Installation successful!')
"
```

### Method 2: Docker Compose (Full Stack)

#### 1. Clone and Configure

```bash
git clone https://github.com/sndwch/cliffracer.git
cd cliffracer

# Create environment file
cp .env.example .env
# Edit .env with your configuration
```

#### 2. Start Full Stack

```bash
# Start everything (NATS, Zabbix, PostgreSQL, services)
docker-compose -f docker-compose-monitoring.yml up -d

# Check services
docker-compose -f docker-compose-monitoring.yml ps
```

#### 3. Access Services

- **NATS Monitoring**: http://localhost:8222
- **Zabbix Web UI**: http://localhost:8080 (admin/zabbix)
- **User Service API**: http://localhost:8001/docs
- **Notification Service**: http://localhost:8002/docs

### Method 3: Production Deployment

#### 1. Prepare Environment

```bash
# Create production directory
mkdir -p /opt/cliffracer
cd /opt/cliffracer

# Clone repository
git clone https://github.com/sndwch/cliffracer.git .

# Set up environment
cp .env.production .env
# Configure production settings in .env
```

#### 2. Configure Services

```bash
# Update configuration for production
export NATS_URL="nats://nats-cluster:4222"
export LOG_LEVEL="INFO"
export LOG_FORMAT="json"
export ZABBIX_SERVER="zabbix.example.com"
```

#### 3. Deploy with Docker Compose

```bash
# Start production stack
docker-compose -f docker-compose-monitoring.yml up -d

# Set up reverse proxy (nginx/traefik)
# Configure SSL certificates
# Set up log aggregation
```

## Configuration

### Environment Variables

Create a `.env` file in your project root:

```bash
# NATS Configuration
NATS_URL=nats://localhost:4222
NATS_CLUSTER_URLS=nats://nats1:4222,nats://nats2:4222,nats://nats3:4222

# Logging Configuration
LOG_LEVEL=INFO
LOG_FORMAT=json  # or "human" for development
LOG_DIR=./logs

# Service Configuration
SERVICE_NAME=my_service
AUTO_RESTART=true
REQUEST_TIMEOUT=30.0

# Monitoring Configuration
ZABBIX_SERVER=zabbix-server
METRICS_INTERVAL=30

# Database Configuration (for your services)
DATABASE_URL=postgresql://user:pass@localhost:5432/dbname

# Security
SECRET_KEY=your-secret-key-here
JWT_SECRET=your-jwt-secret-here
```

### Auto-Activation Setup

The project includes a `.python-version` file that uv automatically uses. No additional setup is required.

```bash
# uv automatically detects and uses the Python version specified in .python-version
# Just run any uv command and it will use the correct Python version
uv run python --version  # Should show Python 3.13.2
```

## Verification

### Test Core Framework

```python
# test_installation.py
import asyncio
from cliffracer import ServiceConfig, NATSService

async def test_service():
    config = ServiceConfig(name="test_service")
    service = NatsService(config)
    
    try:
        await service.connect()
        print("✅ NATS connection successful")
        await service.disconnect()
    except Exception as e:
        print(f"❌ Connection failed: {e}")

if __name__ == "__main__":
    asyncio.run(test_service())
```

```bash
python test_installation.py
```

### Test Extended Features

```python
# test_extended.py
from cliffracer import ValidatedNATSService as ExtendedService, ServiceConfig
from cliffracer import validated_rpc, RPCRequest, RPCResponse
from pydantic import BaseModel

class TestRequest(RPCRequest):
    message: str

class TestResponse(RPCResponse):
    echo: str

class TestService(ExtendedService):
    @validated_rpc(TestRequest, TestResponse)
    async def echo(self, request: TestRequest) -> TestResponse:
        return TestResponse(echo=f"Echo: {request.message}")

print("✅ Extended features imported successfully")
```

### Test Monitoring

```bash
# Check NATS server
curl http://localhost:8222/varz

# Check Zabbix (if running)
curl http://localhost:8080

# Check logs directory
ls -la logs/
```

## Next Steps

1. **[Quick Start](quickstart.md)**: Build your first service
2. **[Configuration](configuration.md)**: Customize the framework
3. **[Examples](../examples/basic-services.md)**: See working examples

## Troubleshooting

### Common Issues

#### NATS Connection Failed

```bash
# Check if NATS is running
docker ps | grep nats

# Check NATS logs
docker logs nats-server

# Test connectivity
telnet localhost 4222
```

#### Import Errors

```bash
# Verify Python environment
which python
python --version

# Check installed packages
pip list | grep -E "(nats|pydantic|fastapi)"

# Reinstall if needed
pip install --upgrade -r requirements-monitoring.txt
```

#### Permission Errors

```bash
# Fix log directory permissions
sudo chown -R $USER:$USER logs/

# Fix Docker socket permissions (Linux)
sudo usermod -aG docker $USER
newgrp docker
```

#### Memory Issues

```bash
# Increase Docker memory limit
# Docker Desktop: Settings > Resources > Memory > 4GB+

# For production, monitor with:
docker stats
```

### Getting Help

- **Documentation**: Check the [troubleshooting section](../deployment/production.md#troubleshooting)
- **Logs**: Enable debug logging with `LOG_LEVEL=DEBUG`
- **Community**: Ask questions in [GitHub Discussions](https://github.com/sndwch/cliffracer/discussions)
- **Issues**: Report bugs in [GitHub Issues](https://github.com/sndwch/cliffracer/issues)