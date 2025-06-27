#!/bin/bash

# Start Test Services for Cliffracer Load Testing
# This script starts all necessary services for comprehensive load testing

set -e

echo "🚀 Starting Cliffracer Load Testing Environment"
echo "=" * 50

# Colors for output
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m' # No Color

# Check if NATS is running
check_nats() {
    echo "📡 Checking NATS server..."
    if ! nc -z localhost 4222 2>/dev/null; then
        echo -e "${YELLOW}⚠️  NATS server not detected on localhost:4222${NC}"
        echo "Starting NATS server with Docker..."
        
        docker run -d --name nats-load-test \
            -p 4222:4222 \
            -p 8222:8222 \
            nats:latest \
            --jetstream \
            --http_port 8222
        
        echo "⏳ Waiting for NATS to start..."
        sleep 3
        
        if nc -z localhost 4222 2>/dev/null; then
            echo -e "${GREEN}✅ NATS server started successfully${NC}"
        else
            echo -e "${RED}❌ Failed to start NATS server${NC}"
            exit 1
        fi
    else
        echo -e "${GREEN}✅ NATS server is running${NC}"
    fi
}

# Install Python dependencies
install_dependencies() {
    echo "📦 Installing Python dependencies..."
    
    if [ -f "requirements.txt" ]; then
        pip install -r requirements.txt
        echo -e "${GREEN}✅ Dependencies installed${NC}"
    else
        echo -e "${YELLOW}⚠️  requirements.txt not found, installing manually...${NC}"
        pip install locust nats-py pydantic faker
    fi
}

# Start Cliffracer test services
start_cliffracer_services() {
    echo "🔧 Starting Cliffracer test services..."
    
    # Export Python path for shared modules
    export PYTHONPATH="${PWD}:${PWD}/services:${PWD}/shared:${PYTHONPATH}"
    
    # Start Order Processing Service in background
    echo "📦 Starting Order Processing Service..."
    cd services/cliffracer-services
    python order_service.py &
    ORDER_SERVICE_PID=$!
    cd ../..
    
    # Store PID for cleanup
    echo $ORDER_SERVICE_PID > .order_service.pid
    
    echo "⏳ Waiting for services to initialize..."
    sleep 5
    
    # Check if service is responsive
    python -c "
import asyncio
import nats
import json
import sys

async def test_connection():
    try:
        nc = await nats.connect('nats://localhost:4222')
        response = await nc.request(
            'order_processing_service.get_service_metrics',
            json.dumps({}).encode(),
            timeout=5.0
        )
        await nc.close()
        print('✅ Order Processing Service is responsive')
        return True
    except Exception as e:
        print(f'❌ Service not responding: {e}')
        return False

if not asyncio.run(test_connection()):
    sys.exit(1)
    "
    
    if [ $? -eq 0 ]; then
        echo -e "${GREEN}✅ Cliffracer services started successfully${NC}"
    else
        echo -e "${RED}❌ Failed to start Cliffracer services${NC}"
        cleanup_services
        exit 1
    fi
}

# Cleanup function
cleanup_services() {
    echo "🧹 Cleaning up services..."
    
    # Kill order service if PID file exists
    if [ -f ".order_service.pid" ]; then
        ORDER_SERVICE_PID=$(cat .order_service.pid)
        if kill -0 $ORDER_SERVICE_PID 2>/dev/null; then
            kill $ORDER_SERVICE_PID
            echo "✅ Order Processing Service stopped"
        fi
        rm .order_service.pid
    fi
    
    # Stop NATS container if we started it
    if docker ps | grep -q "nats-load-test"; then
        docker stop nats-load-test
        docker rm nats-load-test
        echo "✅ NATS server stopped"
    fi
}

# Handle cleanup on script exit
trap cleanup_services EXIT

# Main execution
main() {
    echo "🎯 Starting load testing environment setup..."
    echo
    
    # Change to script directory
    cd "$(dirname "$0")/.."
    
    # Setup steps
    check_nats
    install_dependencies
    start_cliffracer_services
    
    echo
    echo -e "${GREEN}🎉 Load testing environment ready!${NC}"
    echo
    echo "📋 What's running:"
    echo "   📡 NATS server: localhost:4222"
    echo "   📦 Order Processing Service: Ready for RPC calls"
    echo "   🌐 NATS monitoring: http://localhost:8222"
    echo
    echo "🚀 Ready to run load tests!"
    echo
    echo "💡 Next steps:"
    echo "   1. Run basic load test:"
    echo "      locust -f tests/locust/cliffracer_load_test.py --host=nats://localhost:4222"
    echo
    echo "   2. Run web UI load test:"
    echo "      locust -f tests/locust/cliffracer_load_test.py --host=nats://localhost:4222 --web-host=0.0.0.0"
    echo "      Then open: http://localhost:8089"
    echo
    echo "   3. Run headless benchmark:"
    echo "      ./scripts/run-benchmarks.sh"
    echo
    echo "Press Ctrl+C to stop all services and cleanup"
    
    # Keep services running until user interrupts
    while true; do
        sleep 1
    done
}

# Handle command line arguments
case "${1:-}" in
    "cleanup")
        cleanup_services
        exit 0
        ;;
    "check")
        check_nats
        exit 0
        ;;
    *)
        main
        ;;
esac