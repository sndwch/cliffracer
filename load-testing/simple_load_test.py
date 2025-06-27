#!/usr/bin/env python3
"""
Simple Locust load test for NATS performance validation.
Tests basic sub-millisecond response times.
"""

import json
import random
import time
import nats
from locust import User, task, between, events
from locust.exception import StopUser
import gevent
from gevent import monkey
monkey.patch_all()

class SimpleNATSUser(User):
    """Simple NATS user for baseline performance testing."""
    
    wait_time = between(0.01, 0.1)  # Very fast for sub-ms testing
    
    def __init__(self, environment):
        super().__init__(environment)
        self.nats_client = None
        
    def on_start(self):
        """Connect to NATS."""
        async def connect():
            try:
                self.nats_client = await nats.connect("nats://localhost:4222")
                print(f"✅ User {id(self)} connected to NATS")
            except Exception as e:
                print(f"❌ Failed to connect to NATS: {e}")
                raise StopUser()
        
        gevent.spawn(self._run_async(connect)).get()
    
    def on_stop(self):
        """Disconnect from NATS."""
        async def disconnect():
            if self.nats_client:
                await self.nats_client.close()
        
        if self.nats_client:
            gevent.spawn(self._run_async(disconnect)).get()
    
    def _run_async(self, coro):
        """Helper to run async coroutines with gevent."""
        import asyncio
        
        def run_in_thread():
            loop = asyncio.new_event_loop()
            asyncio.set_event_loop(loop)
            try:
                return loop.run_until_complete(coro)
            finally:
                loop.close()
        
        return run_in_thread
    
    def nats_request(self, subject: str, data: dict = None, timeout: float = 1.0):
        """Make a NATS request and measure performance."""
        async def async_request():
            start_time = time.time()
            success = False
            response_data = None
            error_message = None
            
            try:
                request_data = json.dumps(data or {}).encode()
                response = await self.nats_client.request(subject, request_data, timeout=timeout)
                response_data = json.loads(response.data.decode())
                success = True
            except Exception as e:
                error_message = str(e)
            
            response_time = (time.time() - start_time) * 1000  # Convert to ms
            
            # Fire Locust events
            if success:
                events.request.fire(
                    request_type="NATS",
                    name=subject,
                    response_time=response_time,
                    response_length=len(json.dumps(response_data)) if response_data else 0,
                )
            else:
                events.request.fire(
                    request_type="NATS",
                    name=subject,
                    response_time=response_time,
                    response_length=0,
                    exception=Exception(error_message or "Unknown error")
                )
            
            return response_data
        
        return gevent.spawn(self._run_async(async_request)).get()
    
    @task(10)
    def ping_test(self):
        """Test simple ping/pong - should be sub-millisecond."""
        self.nats_request("test.ping")
    
    @task(5)
    def echo_test(self):
        """Test echo with small payload."""
        data = {"message": "test", "number": random.randint(1, 100)}
        self.nats_request("test.echo", data)
    
    @task(2)
    def compute_test(self):
        """Test light computation."""
        data = {"numbers": [random.randint(1, 10) for _ in range(5)]}
        self.nats_request("test.compute", data)

@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    print("🔥 Starting NATS performance validation test...")

@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    print("✅ NATS performance test completed!")
    stats = environment.stats.total
    print(f"📊 Results:")
    print(f"   • Total requests: {stats.num_requests}")
    print(f"   • Failed requests: {stats.num_failures}")
    print(f"   • Median response time: {stats.median_response_time}ms")
    print(f"   • 95th percentile: {stats.get_response_time_percentile(0.95)}ms")
    print(f"   • Requests per second: {stats.current_rps:.1f}")
    
    # Check sub-millisecond claim
    if stats.median_response_time < 1.0:
        print("🎉 SUB-MILLISECOND PERFORMANCE ACHIEVED!")
    else:
        print(f"⚠️  Median response time: {stats.median_response_time}ms (>1ms)")

if __name__ == "__main__":
    import subprocess
    import sys
    
    print("🚀 Running simple NATS performance validation")
    subprocess.run([
        sys.executable, "-m", "locust",
        "-f", __file__,
        "--host=nats://localhost:4222",
        "--headless",
        "--users", "50",
        "--spawn-rate", "10",
        "--run-time", "30s"
    ])