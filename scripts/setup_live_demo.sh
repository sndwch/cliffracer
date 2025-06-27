#!/bin/bash

# Cliffracer Live Demo Setup Script
# This script sets up the complete monitoring environment and runs the e-commerce demo

set -e

echo "🚀 Cliffracer - Live Demo Setup"
echo "============================================================="

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m' # No Color

# Function to print colored output
print_status() {
    echo -e "${GREEN}[INFO]${NC} $1"
}

print_warning() {
    echo -e "${YELLOW}[WARN]${NC} $1"
}

print_error() {
    echo -e "${RED}[ERROR]${NC} $1"
}

print_header() {
    echo -e "${BLUE}$1${NC}"
}

# Check if Docker is running
if ! docker info >/dev/null 2>&1; then
    print_error "Docker is not running. Please start Docker Desktop and try again."
    exit 1
fi

# Check if docker-compose is available
if ! command -v docker-compose >/dev/null 2>&1; then
    print_error "docker-compose is not installed. Please install it and try again."
    exit 1
fi

# Check if uv is available
if ! command -v uv >/dev/null 2>&1; then
    print_error "uv is not installed. Please install it with: curl -LsSf https://astral.sh/uv/install.sh | sh"
    exit 1
fi

print_header "📋 Phase 1: Environment Setup"

# Create monitoring directories if they don't exist
print_status "Creating monitoring directories..."
mkdir -p monitoring/grafana/{dashboards,datasources}
mkdir -p monitoring/zabbix/templates

# Create Grafana datasource configuration
cat > monitoring/grafana/datasources/postgres.yml << 'EOF'
apiVersion: 1

datasources:
  - name: PostgreSQL
    type: postgres
    access: proxy
    url: postgres:5432
    database: zabbix
    user: zabbix
    secureJsonData:
      password: zabbix_password
    jsonData:
      sslmode: disable
      maxOpenConns: 0
      maxIdleConns: 2
      connMaxLifetime: 14400
EOF

# Create Grafana dashboard for Cliffracer services
cat > monitoring/grafana/dashboards/cliffracer-services.json << 'EOF'
{
  "dashboard": {
    "id": null,
    "title": "Cliffracer Services Dashboard",
    "tags": ["cliffracer", "microservices"],
    "timezone": "browser",
    "panels": [
      {
        "title": "Service Health",
        "type": "stat",
        "targets": [
          {
            "expr": "up",
            "legendFormat": "{{service}}"
          }
        ]
      }
    ],
    "time": {
      "from": "now-1h",
      "to": "now"
    },
    "refresh": "5s"
  }
}
EOF

print_header "🐳 Phase 2: Starting Infrastructure Services"

# Stop any existing containers
print_status "Stopping existing containers..."
docker-compose -f docker-compose-live-demo.yml down >/dev/null 2>&1 || true

# Start infrastructure services
print_status "Starting NATS, PostgreSQL, and monitoring services..."
docker-compose -f docker-compose-live-demo.yml up -d

print_status "Waiting for services to be healthy..."

# Wait for NATS to be ready
print_status "Waiting for NATS server..."
timeout=60
counter=0
while ! curl -s http://localhost:8222/varz >/dev/null 2>&1; do
    if [ $counter -ge $timeout ]; then
        print_error "NATS server failed to start within $timeout seconds"
        exit 1
    fi
    sleep 2
    counter=$((counter + 2))
    echo -n "."
done
echo ""
print_status "✅ NATS server is ready!"

# Wait for PostgreSQL to be ready
print_status "Waiting for PostgreSQL..."
timeout=60
counter=0
while ! docker exec cliffracer-postgres pg_isready -U zabbix -d zabbix >/dev/null 2>&1; do
    if [ $counter -ge $timeout ]; then
        print_error "PostgreSQL failed to start within $timeout seconds"
        exit 1
    fi
    sleep 2
    counter=$((counter + 2))
    echo -n "."
done
echo ""
print_status "✅ PostgreSQL is ready!"

# Wait for Zabbix Web to be ready
print_status "Waiting for Zabbix web interface..."
timeout=120
counter=0
while ! curl -s http://localhost:8080 >/dev/null 2>&1; do
    if [ $counter -ge $timeout ]; then
        print_error "Zabbix web interface failed to start within $timeout seconds"
        exit 1
    fi
    sleep 3
    counter=$((counter + 3))
    echo -n "."
done
echo ""
print_status "✅ Zabbix web interface is ready!"

print_header "🔧 Phase 3: Python Environment Setup"

# Check if we're in the right directory
if [ ! -f "pyproject.toml" ]; then
    print_error "pyproject.toml not found. Please run this script from the cliffracer directory."
    exit 1
fi

# Install Python dependencies
print_status "Installing Python dependencies with uv..."
if ! uv sync --extra all; then
    print_error "Failed to install dependencies. Please check your Python environment."
    exit 1
fi

print_header "📊 Phase 4: Service Information"

print_status "All services are now running! Here's how to access everything:"
echo ""
echo "🌐 Web Interfaces:"
echo "  📊 Zabbix Dashboard:    http://localhost:8080"
echo "     Username: Admin / Password: zabbix"
echo ""
echo "  📈 NATS Monitoring:     http://localhost:8222"
echo "  📊 Grafana (optional):  http://localhost:3000" 
echo "     Username: admin / Password: admin"
echo ""
echo "🔌 API Endpoints:"
echo "  🛒 Order Service API:   http://localhost:8001/docs"
echo "  📦 Service Metrics:     http://localhost:8001/metrics"
echo ""
echo "🔧 Infrastructure:"
echo "  💾 NATS Client Port:    localhost:4222"
echo "  🗄️  PostgreSQL:          localhost:5432"
echo "  📱 Redis:               localhost:6379"
echo ""

print_header "🚀 Phase 5: Starting Cliffracer Services"

print_status "Starting the e-commerce demo system..."
echo ""
echo "This will start:"
echo "  • Order Service (with HTTP API)"
echo "  • Inventory Service"  
echo "  • Payment Service"
echo "  • Notification Service"
echo "  • Load Generator (creates orders every 2-10 seconds)"
echo ""
echo "📊 Watch the monitoring dashboards while the system runs!"
echo "🔄 The load generator will create realistic e-commerce traffic"
echo "📝 All services log structured JSON for easy monitoring"
echo ""
echo "Press Ctrl+C to stop all services when you're done."
echo ""

read -p "Press Enter to start the Cliffracer services..." 

# Start the Python services
print_status "🚀 Launching Cliffracer e-commerce system..."
uv run python example_ecommerce_live.py