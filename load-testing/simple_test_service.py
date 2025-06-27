#!/usr/bin/env python3
"""
Simple test service for Cliffracer load testing validation.
Uses basic NATS messaging to demonstrate sub-millisecond performance.
"""

import asyncio
import json
import time
from datetime import datetime
import nats

class SimpleTestService:
    def __init__(self):
        self.nc = None
        self.start_time = time.time()
        self.request_count = 0

    async def start(self):
        """Start the service and connect to NATS."""
        self.nc = await nats.connect("nats://localhost:4222")
        
        # Subscribe to test subjects
        await self.nc.subscribe("test.ping", cb=self.handle_ping)
        await self.nc.subscribe("test.echo", cb=self.handle_echo)
        await self.nc.subscribe("test.compute", cb=self.handle_compute)
        
        print("🚀 Simple Test Service started")
        print("   • test.ping - Simple ping/pong")
        print("   • test.echo - Echo back request data")
        print("   • test.compute - Light computation test")
        
    async def handle_ping(self, msg):
        """Handle ping requests - minimal processing."""
        self.request_count += 1
        response = {
            "pong": True,
            "timestamp": time.time(),
            "request_id": self.request_count
        }
        await msg.respond(json.dumps(response).encode())
    
    async def handle_echo(self, msg):
        """Handle echo requests - return the data."""
        self.request_count += 1
        try:
            data = json.loads(msg.data.decode())
            response = {
                "echo": data,
                "timestamp": time.time(),
                "request_id": self.request_count
            }
        except Exception as e:
            response = {
                "error": str(e),
                "timestamp": time.time(),
                "request_id": self.request_count
            }
        await msg.respond(json.dumps(response).encode())
    
    async def handle_compute(self, msg):
        """Handle computation requests - light processing."""
        self.request_count += 1
        try:
            data = json.loads(msg.data.decode())
            
            # Light computation to simulate business logic
            numbers = data.get("numbers", [1, 2, 3, 4, 5])
            result = sum(x * x for x in numbers)
            
            # Simulate some validation
            await asyncio.sleep(0.0001)  # 0.1ms simulated processing
            
            response = {
                "result": result,
                "count": len(numbers),
                "timestamp": time.time(),
                "request_id": self.request_count,
                "uptime": time.time() - self.start_time
            }
        except Exception as e:
            response = {
                "error": str(e),
                "timestamp": time.time(),
                "request_id": self.request_count
            }
        await msg.respond(json.dumps(response).encode())

    async def stop(self):
        """Stop the service."""
        if self.nc:
            await self.nc.close()
            print("✅ Simple Test Service stopped")

async def main():
    service = SimpleTestService()
    await service.start()
    
    try:
        # Keep running until interrupted
        while True:
            await asyncio.sleep(1)
    except KeyboardInterrupt:
        print("\n👋 Shutting down...")
        await service.stop()

if __name__ == "__main__":
    asyncio.run(main())