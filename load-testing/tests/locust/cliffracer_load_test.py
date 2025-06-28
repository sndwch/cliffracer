"""
Comprehensive Locust load test for Cliffracer services.

Tests various scenarios including:
- Complex object validation under load
- Error handling resilience
- High-frequency event processing
- Large payload processing
- Concurrent service communication
"""

import asyncio
import json
import os
import random

# Import our test data generators
import sys
import time

import nats
from locust import User, between, events, task
from locust.exception import StopUser

sys.path.append(os.path.join(os.path.dirname(__file__), "../../"))

from shared.generators import (
    DataGenerator,
    generate_analytics_batch,
    generate_error_test_data,
    generate_test_orders,
)


class NATSUser(User):
    """
    Base NATS user for Cliffracer load testing.

    Handles NATS connection, RPC calls, and performance measurement.
    """

    wait_time = between(0.1, 1.0)  # Wait 0.1-1.0 seconds between tasks

    def __init__(self, environment):
        super().__init__(environment)
        self.nats_client = None
        self.data_generator = DataGenerator(error_rate=0.05)  # 5% error rate

    async def on_start(self):
        """Connect to NATS when user starts."""
        try:
            self.nats_client = await nats.connect("nats://localhost:4222")
            print(f"✅ User {id(self)} connected to NATS")
        except Exception as e:
            print(f"❌ Failed to connect to NATS: {e}")
            raise StopUser()

    async def on_stop(self):
        """Disconnect from NATS when user stops."""
        if self.nats_client:
            await self.nats_client.close()
            print(f"👋 User {id(self)} disconnected from NATS")

    async def nats_rpc_call(self, subject: str, data: dict, timeout: float = 5.0):
        """
        Make an RPC call via NATS and measure performance.

        Args:
            subject: NATS subject for the RPC call
            data: Data to send
            timeout: Request timeout in seconds

        Returns:
            Response data or None if failed
        """
        start_time = time.time()
        response_data = None
        success = False
        error_message = None

        try:
            # Serialize data to JSON
            request_data = json.dumps(data, default=str).encode()

            # Make RPC call
            response = await self.nats_client.request(subject, request_data, timeout=timeout)

            # Parse response
            response_data = json.loads(response.data.decode())

            # Check if business logic succeeded
            if isinstance(response_data, dict) and response_data.get("success", True):
                success = True
            else:
                success = False
                error_message = response_data.get("error", "Business logic failure")

        except TimeoutError:
            error_message = "Request timeout"
            success = False
        except Exception as e:
            error_message = str(e)
            success = False

        # Calculate response time
        response_time = (time.time() - start_time) * 1000  # Convert to ms

        # Fire Locust events for metrics
        if success:
            events.request.fire(
                request_type="NATS-RPC",
                name=subject,
                response_time=response_time,
                response_length=len(json.dumps(response_data)) if response_data else 0,
            )
        else:
            events.request.fire(
                request_type="NATS-RPC",
                name=subject,
                response_time=response_time,
                response_length=0,
                exception=Exception(error_message or "Unknown error"),
            )

        return response_data

    def run_async_task(self, coro):
        """Helper to run async tasks in Locust."""
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        try:
            return loop.run_until_complete(coro)
        finally:
            loop.close()


class OrderProcessingUser(NATSUser):
    """
    User simulating order processing workload.

    Tests complex object validation, business logic, and error scenarios.
    """

    weight = 3  # Higher weight = more users of this type

    @task(10)
    def process_single_order(self):
        """Process a single complex order - main workload."""
        # Generate a complex order with realistic data
        order = self.data_generator.generate_complex_order()

        # Convert to dict for JSON serialization
        order_data = order.dict()

        # Make RPC call
        response = self.run_async_task(
            self.nats_rpc_call("order_processing_service.process_order", order_data)
        )

        # Optional: Validate response structure
        if response and isinstance(response, dict):
            if not response.get("success", False):
                # This is expected for some orders (business rule failures, etc.)
                pass

    @task(3)
    def process_order_batch(self):
        """Process multiple orders in a batch - tests concurrent processing."""
        # Generate 5-15 orders for batch processing
        batch_size = random.randint(5, 15)
        orders = generate_test_orders(batch_size, error_rate=0.03)

        # Convert to dict list
        orders_data = [order.dict() for order in orders]

        # Make batch RPC call
        response = self.run_async_task(
            self.nats_rpc_call(
                "order_processing_service.batch_process_orders",
                orders_data,
                timeout=10.0,  # Longer timeout for batch processing
            )
        )

        # Log batch processing metrics
        if response and isinstance(response, dict):
            batch_metrics = {
                "total_orders": response.get("total_orders", 0),
                "successful": response.get("successful", 0),
                "failed": response.get("failed", 0),
                "orders_per_second": response.get("orders_per_second", 0),
            }
            print(f"📊 Batch processed: {batch_metrics}")

    @task(1)
    def get_service_metrics(self):
        """Get service performance metrics - low frequency monitoring."""
        response = self.run_async_task(
            self.nats_rpc_call("order_processing_service.get_service_metrics", {})
        )

        if response:
            print(f"📈 Service metrics: {response}")


class AnalyticsUser(NATSUser):
    """
    User simulating high-frequency analytics event processing.

    Tests throughput and performance under sustained high-frequency load.
    """

    weight = 2
    wait_time = between(0.05, 0.2)  # Much faster for analytics events

    @task(15)
    def send_analytics_events(self):
        """Send batch of analytics events - high frequency."""
        # Generate 10-50 events per batch
        batch_size = random.randint(10, 50)
        events = generate_analytics_batch(batch_size)

        # Convert to dict list
        events_data = [event.dict() for event in events]

        # Send to analytics ingestion
        response = self.run_async_task(
            self.nats_rpc_call(
                "order_processing_service.analytics_ingestion", events_data, timeout=3.0
            )
        )

        # Track analytics performance
        if response and isinstance(response, dict):
            events_per_second = response.get("events_per_second", 0)
            if events_per_second > 0:
                # This represents high-frequency processing success
                pass


class LargePayloadUser(NATSUser):
    """
    User testing large payload processing and memory efficiency.

    Tests serialization performance and memory usage under load.
    """

    weight = 1
    wait_time = between(2.0, 5.0)  # Slower for large payloads

    @task(5)
    def process_large_batch(self):
        """Process large batch requests - tests memory and serialization."""
        # Generate large batch (500-2000 items)
        batch_size = random.randint(500, 2000)
        batch_request = self.data_generator.generate_batch_request(batch_size)

        # Convert to dict
        batch_data = batch_request.dict()

        # Process large payload
        response = self.run_async_task(
            self.nats_rpc_call(
                "order_processing_service.large_payload_processing",
                batch_data,
                timeout=15.0,  # Longer timeout for large payloads
            )
        )

        # Log memory efficiency metrics
        if response and isinstance(response, dict):
            efficiency_metrics = {
                "items_processed": response.get("processed_items", 0),
                "items_per_second": response.get("items_per_second", 0),
                "payload_size_mb": response.get("estimated_payload_size_mb", 0),
                "memory_efficiency": response.get("memory_efficiency_items_per_mb", 0),
            }
            print(f"💾 Large payload metrics: {efficiency_metrics}")


class ErrorScenarioUser(NATSUser):
    """
    User that intentionally triggers validation errors.

    Tests error handling performance and ensures RPS doesn't degrade
    when errors occur.
    """

    weight = 1
    wait_time = between(0.5, 1.5)

    @task(8)
    def trigger_validation_errors(self):
        """Send invalid data to test error handling performance."""
        # Generate data that will definitely trigger validation errors
        error_data_list = generate_error_test_data(1)
        error_data = error_data_list[0].dict()

        # This should fail validation but should not impact service performance
        response = self.run_async_task(
            self.nats_rpc_call(
                "order_processing_service.error_scenario_test", error_data, timeout=2.0
            )
        )

        # For error scenarios, we expect failure - that's success!
        if response and isinstance(response, dict):
            validation_failed = response.get("validation_failed", False)
            if validation_failed:
                # This is the expected outcome
                pass

    @task(2)
    def trigger_business_logic_errors(self):
        """Trigger business logic errors (not validation errors)."""
        # Generate an order that will fail business rules
        order = self.data_generator.generate_complex_order(force_error=True)
        order_data = order.dict()

        # This should process but fail business validation
        response = self.run_async_task(
            self.nats_rpc_call("order_processing_service.process_order", order_data)
        )

        # Business logic errors are also "successful" error handling
        if response and isinstance(response, dict):
            if not response.get("success", True):
                # Expected business logic failure
                pass


class MixedWorkloadUser(NATSUser):
    """
    User that simulates a realistic mixed workload.

    Combines different types of requests to simulate real-world usage patterns.
    """

    weight = 2

    @task(5)
    def realistic_order_processing(self):
        """Realistic order processing with mixed complexity."""
        # 80% simple orders, 20% complex orders
        if random.random() < 0.8:
            # Simple order (fewer items, basic customer)
            order = self.data_generator.generate_complex_order(num_items=random.randint(1, 3))
        else:
            # Complex order (more items, complex customer data)
            order = self.data_generator.generate_complex_order(num_items=random.randint(5, 12))

        order_data = order.dict()

        response = self.run_async_task(
            self.nats_rpc_call("order_processing_service.process_order", order_data)
        )

    @task(3)
    def mixed_analytics_load(self):
        """Mixed analytics processing with variable batch sizes."""
        # Variable batch sizes: 5-100 events
        batch_size = random.choice([5, 10, 20, 50, 100])
        events = generate_analytics_batch(batch_size)
        events_data = [event.dict() for event in events]

        response = self.run_async_task(
            self.nats_rpc_call("order_processing_service.analytics_ingestion", events_data)
        )

    @task(1)
    def occasional_large_payload(self):
        """Occasional large payload processing."""
        # Small chance of very large payload
        if random.random() < 0.1:  # 10% chance
            batch_size = random.randint(1000, 5000)
        else:
            batch_size = random.randint(100, 500)

        batch_request = self.data_generator.generate_batch_request(batch_size)
        batch_data = batch_request.dict()

        response = self.run_async_task(
            self.nats_rpc_call(
                "order_processing_service.large_payload_processing", batch_data, timeout=20.0
            )
        )


# Locust event handlers for additional metrics


@events.init.add_listener
def on_locust_init(environment, **kwargs):
    """Initialize custom metrics when Locust starts."""
    print("🚀 Starting Cliffracer Load Test")
    print("=" * 50)
    print("📊 Test scenarios:")
    print("   • OrderProcessingUser: Complex order validation and processing")
    print("   • AnalyticsUser: High-frequency event ingestion")
    print("   • LargePayloadUser: Memory and serialization testing")
    print("   • ErrorScenarioUser: Error handling resilience")
    print("   • MixedWorkloadUser: Realistic mixed workload")
    print()


@events.test_start.add_listener
def on_test_start(environment, **kwargs):
    """Log test start."""
    print(f"🔥 Load test started with {environment.runner.user_count} users")


@events.test_stop.add_listener
def on_test_stop(environment, **kwargs):
    """Log test completion and summary."""
    print("✅ Load test completed!")
    print(f"📈 Total requests: {environment.stats.total.num_requests}")
    print(f"📈 Failed requests: {environment.stats.total.num_failures}")
    print(f"📈 Median response time: {environment.stats.total.median_response_time}ms")
    print(f"📈 95th percentile: {environment.stats.total.get_response_time_percentile(0.95)}ms")
    print(f"📈 Requests per second: {environment.stats.total.current_rps}")


# Configuration for running the test
if __name__ == "__main__":
    # This allows running the test directly with: python cliffracer_load_test.py
    import subprocess
    import sys

    print("🚀 Starting Cliffracer Load Test with Locust")
    print("To run manually, use:")
    print("locust -f cliffracer_load_test.py --host=nats://localhost:4222")

    # Auto-run with reasonable defaults
    subprocess.run(
        [
            sys.executable,
            "-m",
            "locust",
            "-f",
            __file__,
            "--host=nats://localhost:4222",
            "--headless",
            "--users",
            "50",
            "--spawn-rate",
            "5",
            "--run-time",
            "60s",
        ]
    )
