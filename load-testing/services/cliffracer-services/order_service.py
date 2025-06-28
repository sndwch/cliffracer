#!/usr/bin/env python3
"""
Cliffracer Order Processing Service for Load Testing

Complex service with heavy validation, error scenarios, and realistic business logic.
Designed to stress-test Cliffracer performance under various conditions.
"""

import asyncio
import os
import random

# Import our complex test models
import sys
from datetime import UTC, datetime
from decimal import Decimal

from cliffracer import ServiceConfig, ValidatedNATSService

sys.path.append(os.path.join(os.path.dirname(__file__), "../../"))
sys.path.append(os.path.join(os.path.dirname(__file__), "../../../src"))

from shared.generators import DataGenerator
from shared.models import (
    AnalyticsEvent,
    BatchProcessingRequest,
    ComplexOrder,
    CustomerProfile,
    OrderItem,
    ValidationErrorTest,
)


class OrderProcessingService(ValidatedNATSService):
    """
    High-performance order processing service for load testing.

    Features complex validation, error handling, and realistic business logic
    to provide comprehensive performance testing scenarios.
    """

    def __init__(self, config: ServiceConfig):
        super().__init__(config)
        self.processed_orders = 0
        self.failed_orders = 0
        self.total_revenue = Decimal("0.00")
        self.processing_times = []
        self.data_generator = DataGenerator(error_rate=0.05)

        # Simulate some persistent state
        self.customer_cache: dict[str, CustomerProfile] = {}
        self.inventory: dict[str, int] = {}
        self.fraud_scores: dict[str, float] = {}

    # @validated_rpc  # Simplified for load testing
    async def process_order(self, order: ComplexOrder) -> dict[str, any]:
        """
        Process a complex order with full validation and business logic.

        This method includes:
        - Complex object validation (automatic via Pydantic)
        - Business rule validation
        - Inventory checks
        - Fraud detection simulation
        - Database simulation (async delays)
        - Error scenarios
        """
        start_time = datetime.now(UTC)

        try:
            # Simulate fraud detection (CPU-intensive)
            fraud_score = await self._calculate_fraud_score(order)
            if fraud_score > 0.8:
                self.failed_orders += 1
                return {
                    "success": False,
                    "order_id": order.order_id,
                    "error": "Order flagged for fraud review",
                    "fraud_score": fraud_score,
                }

            # Simulate inventory validation (with potential failures)
            inventory_check = await self._validate_inventory(order.items)
            if not inventory_check["success"]:
                self.failed_orders += 1
                return {
                    "success": False,
                    "order_id": order.order_id,
                    "error": f"Insufficient inventory: {inventory_check['missing_items']}",
                    "available_inventory": inventory_check["available"],
                }

            # Simulate complex business rules validation
            validation_result = await self._validate_business_rules(order)
            if not validation_result["valid"]:
                self.failed_orders += 1
                return {
                    "success": False,
                    "order_id": order.order_id,
                    "error": f"Business rule violation: {validation_result['reason']}",
                    "rule_violations": validation_result["violations"],
                }

            # Simulate database operations (async I/O)
            await self._save_order_to_database(order)
            await self._update_customer_profile(order.customer)
            await self._reserve_inventory(order.items)

            # Update service metrics
            self.processed_orders += 1
            self.total_revenue += order.total_amount

            # Record processing time for performance monitoring
            processing_time = (datetime.now(UTC) - start_time).total_seconds()
            self.processing_times.append(processing_time)

            # Keep only last 1000 processing times for memory efficiency
            if len(self.processing_times) > 1000:
                self.processing_times = self.processing_times[-1000:]

            return {
                "success": True,
                "order_id": order.order_id,
                "status": "processed",
                "estimated_ship_date": order.expected_ship_date.isoformat()
                if order.expected_ship_date
                else None,
                "tracking_number": order.shipping.tracking_number,
                "total_amount": float(order.total_amount),
                "processing_time_ms": processing_time * 1000,
                "fraud_score": fraud_score,
            }

        except Exception as e:
            self.failed_orders += 1
            return {
                "success": False,
                "order_id": order.order_id,
                "error": f"Processing error: {str(e)}",
                "error_type": type(e).__name__,
            }

    # @validated_rpc  # Simplified for load testing
    async def batch_process_orders(self, orders: list[ComplexOrder]) -> dict[str, any]:
        """
        Process multiple orders in a batch for throughput testing.

        Tests concurrent processing capabilities and batch optimization.
        """
        start_time = datetime.now(UTC)
        results = []

        # Process orders concurrently for better performance
        tasks = [self.process_order(order) for order in orders]
        batch_results = await asyncio.gather(*tasks, return_exceptions=True)

        successful = 0
        failed = 0

        for i, result in enumerate(batch_results):
            if isinstance(result, Exception):
                failed += 1
                results.append(
                    {"order_id": orders[i].order_id, "success": False, "error": str(result)}
                )
            else:
                if result.get("success", False):
                    successful += 1
                else:
                    failed += 1
                results.append(result)

        processing_time = (datetime.now(UTC) - start_time).total_seconds()

        return {
            "batch_id": f"BATCH-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}",
            "total_orders": len(orders),
            "successful": successful,
            "failed": failed,
            "processing_time_seconds": processing_time,
            "orders_per_second": len(orders) / processing_time if processing_time > 0 else 0,
            "results": results,
        }

    # @validated_rpc  # Simplified for load testing
    async def analytics_ingestion(self, events: list[AnalyticsEvent]) -> dict[str, any]:
        """
        High-frequency analytics event ingestion for throughput testing.

        Simulates real-time analytics processing with complex aggregations.
        """
        start_time = datetime.now(UTC)

        # Simulate complex analytics processing
        event_counts = {}
        user_sessions = {}
        value_sum = 0.0

        for event in events:
            # Count events by type
            event_counts[event.event_type] = event_counts.get(event.event_type, 0) + 1

            # Track user sessions
            if event.user_id:
                if event.user_id not in user_sessions:
                    user_sessions[event.user_id] = []
                user_sessions[event.user_id].append(event.event_type)

            # Sum values
            if "value" in event.properties:
                value_sum += event.properties["value"]

        # Simulate database writes (async delays)
        await asyncio.sleep(0.001 * len(events))  # 1ms per event

        processing_time = (datetime.now(UTC) - start_time).total_seconds()

        return {
            "events_processed": len(events),
            "processing_time_seconds": processing_time,
            "events_per_second": len(events) / processing_time if processing_time > 0 else 0,
            "event_type_counts": event_counts,
            "unique_users": len(user_sessions),
            "total_value": value_sum,
            "average_value": value_sum / len(events) if events else 0,
        }

    # @validated_rpc  # Simplified for load testing
    async def large_payload_processing(
        self, batch_request: BatchProcessingRequest
    ) -> dict[str, any]:
        """
        Process large payloads to test memory usage and serialization performance.
        """
        start_time = datetime.now(UTC)

        # Simulate complex processing on large dataset
        processed_items = 0
        failed_items = 0
        total_size_bytes = 0

        for item in batch_request.items:
            try:
                # Simulate validation and processing
                if "data" in item and "name" in item["data"]:
                    # Simulate some CPU work
                    await asyncio.sleep(0.0001)  # 0.1ms per item
                    processed_items += 1

                    # Estimate item size (simplified)
                    total_size_bytes += len(str(item))
                else:
                    failed_items += 1

            except Exception:
                failed_items += 1

        processing_time = (datetime.now(UTC) - start_time).total_seconds()

        return {
            "batch_id": batch_request.batch_id,
            "operation_type": batch_request.operation_type,
            "total_items": len(batch_request.items),
            "processed_items": processed_items,
            "failed_items": failed_items,
            "processing_time_seconds": processing_time,
            "items_per_second": processed_items / processing_time if processing_time > 0 else 0,
            "estimated_payload_size_mb": total_size_bytes / (1024 * 1024),
            "memory_efficiency_items_per_mb": processed_items / (total_size_bytes / (1024 * 1024))
            if total_size_bytes > 0
            else 0,
        }

    # @validated_rpc  # Simplified for load testing
    async def error_scenario_test(self, test_data: ValidationErrorTest) -> dict[str, any]:
        """
        Test error handling and recovery.

        This method is designed to fail validation frequently to test
        error handling performance and ensure RPS doesn't degrade.
        """
        try:
            # The validation will fail automatically due to invalid data
            # This should never be reached for invalid data
            return {
                "success": True,
                "message": "Validation passed unexpectedly",
                "data": test_data.dict(),
            }
        except Exception as e:
            # This is the expected path for invalid data
            return {
                "success": False,
                "error": str(e),
                "error_type": type(e).__name__,
                "validation_failed": True,
            }

    # @validated_rpc  # Simplified for load testing
    async def get_service_metrics(self) -> dict[str, any]:
        """Get comprehensive service performance metrics."""
        avg_processing_time = (
            sum(self.processing_times) / len(self.processing_times) if self.processing_times else 0
        )

        return {
            "service_name": self.config.name,
            "processed_orders": self.processed_orders,
            "failed_orders": self.failed_orders,
            "success_rate": (
                self.processed_orders / (self.processed_orders + self.failed_orders)
                if (self.processed_orders + self.failed_orders) > 0
                else 0
            ),
            "total_revenue": float(self.total_revenue),
            "average_processing_time_ms": avg_processing_time * 1000,
            "cached_customers": len(self.customer_cache),
            "inventory_items": len(self.inventory),
            "recent_processing_times": self.processing_times[-10:],  # Last 10 times
        }

    # Private helper methods for realistic business logic simulation

    async def _calculate_fraud_score(self, order: ComplexOrder) -> float:
        """Simulate CPU-intensive fraud detection."""
        # Simulate complex fraud calculation
        score = 0.0

        # Check order value
        if order.total_amount > Decimal("5000.00"):
            score += 0.2

        # Check customer history (simulate database lookup)
        await asyncio.sleep(0.001)  # 1ms database lookup
        if order.customer.customer_id in self.fraud_scores:
            score += self.fraud_scores[order.customer.customer_id]
        else:
            # New customer - assign random baseline score
            baseline = random.uniform(0.0, 0.3)
            self.fraud_scores[order.customer.customer_id] = baseline
            score += baseline

        # Check payment method risk
        if order.payment.method.value == "crypto":
            score += 0.3

        # Add some randomness for testing
        score += random.uniform(0.0, 0.2)

        return min(score, 1.0)

    async def _validate_inventory(self, items: list[OrderItem]) -> dict[str, any]:
        """Simulate inventory validation with potential failures."""
        missing_items = []
        available = {}

        for item in items:
            # Simulate database lookup
            await asyncio.sleep(0.0005)  # 0.5ms per item lookup

            current_stock = self.inventory.get(item.variant_sku, random.randint(0, 100))
            self.inventory[item.variant_sku] = current_stock
            available[item.variant_sku] = current_stock

            if current_stock < item.quantity:
                missing_items.append(
                    {
                        "sku": item.variant_sku,
                        "requested": item.quantity,
                        "available": current_stock,
                    }
                )

        return {
            "success": len(missing_items) == 0,
            "missing_items": missing_items,
            "available": available,
        }

    async def _validate_business_rules(self, order: ComplexOrder) -> dict[str, any]:
        """Simulate complex business rule validation."""
        violations = []

        # Rule 1: Maximum order value for new customers
        if order.customer.customer_id not in self.customer_cache and order.total_amount > Decimal(
            "1000.00"
        ):
            violations.append("New customer order exceeds maximum value")

        # Rule 2: International shipping restrictions
        if order.shipping.shipping_address.country != "US" and order.total_amount > Decimal(
            "10000.00"
        ):
            violations.append("International orders over $10,000 require special approval")

        # Rule 3: Fraud check requirement
        if not order.fraud_check_passed and order.total_amount > Decimal("500.00"):
            violations.append("Fraud check required for orders over $500")

        # Simulate complex rule processing
        await asyncio.sleep(0.002)  # 2ms for business rule processing

        return {
            "valid": len(violations) == 0,
            "violations": violations,
            "reason": "; ".join(violations) if violations else None,
        }

    async def _save_order_to_database(self, order: ComplexOrder):
        """Simulate database save operation."""
        # Simulate database write time
        await asyncio.sleep(0.005)  # 5ms database write
        # In real implementation, this would save to actual database
        pass

    async def _update_customer_profile(self, customer: CustomerProfile):
        """Simulate customer profile update."""
        await asyncio.sleep(0.002)  # 2ms database update
        self.customer_cache[customer.customer_id] = customer

    async def _reserve_inventory(self, items: list[OrderItem]):
        """Simulate inventory reservation."""
        for item in items:
            if item.variant_sku in self.inventory:
                self.inventory[item.variant_sku] -= item.quantity
        await asyncio.sleep(0.001)  # 1ms inventory update


async def main():
    """Run the order processing service for load testing."""
    print("🚀 Starting Cliffracer Order Processing Service for Load Testing")
    print("=" * 60)

    # Configure logging for performance testing
    # LoggingConfig.configure(level="WARNING")  # Simplified for load testing

    # Create service configuration
    config = ServiceConfig(
        name="order_processing_service",
        nats_url="nats://localhost:4222",
        # Disable backdoor for clean performance testing
        backdoor_enabled=False,
    )

    # Create and start service
    service = OrderProcessingService(config)

    try:
        await service.connect()
        print("✅ Order Processing Service connected and ready for load testing")
        print("📊 Available RPC methods:")
        print("   • process_order(order: ComplexOrder)")
        print("   • batch_process_orders(orders: List[ComplexOrder])")
        print("   • analytics_ingestion(events: List[AnalyticsEvent])")
        print("   • large_payload_processing(batch_request: BatchProcessingRequest)")
        print("   • error_scenario_test(test_data: ValidationErrorTest)")
        print("   • get_service_metrics()")
        print()
        print("🔥 Ready for Locust load testing!")
        print("💡 Use Ctrl+C to stop the service")

        # Keep service running
        while True:
            await asyncio.sleep(1)

    except KeyboardInterrupt:
        print("\\n👋 Shutting down Order Processing Service...")
    except Exception as e:
        print(f"❌ Service error: {e}")
    finally:
        await service.disconnect()
        print("✅ Service stopped")


if __name__ == "__main__":
    asyncio.run(main())
