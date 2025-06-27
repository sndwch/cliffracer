#!/usr/bin/env python3
"""
Simplified Cliffracer Demo (No Docker Required)
==============================================

This runs the e-commerce system with just Python and shows the key features:
- Multiple microservices communicating via NATS
- Structured logging with correlation IDs
- Real-time metrics
- Event-driven architecture

Start NATS separately with: docker run -p 4222:4222 -p 8222:8222 nats:alpine -js -m 8222
Or use an existing NATS server.
"""

import asyncio
import logging
import sys
from example_ecommerce_live import main as run_ecommerce

# Configure simpler logging for demo
logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s',
    stream=sys.stdout
)

print("🚀 Cliffracer NATS Framework - Simplified Demo")
print("=" * 50)
print()
print("This demo runs the complete e-commerce system:")
print("  ✅ Order Service (HTTP API + NATS)")
print("  ✅ Inventory Service")
print("  ✅ Payment Service")
print("  ✅ Notification Service")
print("  ✅ Load Generator")
print()
print("📊 Access Points:")
print("  🌐 Order API: http://localhost:8001/docs")
print("  📈 NATS Monitor: http://localhost:8222 (if NATS running)")
print("  📝 Logs: Watch this terminal for structured events")
print()
print("🔄 The system will generate orders automatically")
print("💡 Watch the logs for business events and metrics")
print()
print("Press Ctrl+C to stop all services")
print("=" * 50)

if __name__ == "__main__":
    try:
        asyncio.run(run_ecommerce())
    except KeyboardInterrupt:
        print("\n🛑 Demo stopped by user")
    except Exception as e:
        print(f"\n❌ Demo error: {e}")
        print("\n💡 Make sure NATS is running: docker run -p 4222:4222 nats:alpine")