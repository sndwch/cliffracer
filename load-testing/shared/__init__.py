"""
Shared components for load testing.

Contains data models, generators, and utilities used across
Cliffracer and comparison framework tests.
"""

from .generators import (
    DataGenerator,
    generate_analytics_batch,
    generate_error_test_data,
    generate_test_orders,
)
from .models import (
    AnalyticsEvent,
    BatchProcessingRequest,
    ComplexOrder,
    CustomerProfile,
    OrderItem,
    Product,
    ValidationErrorTest,
)

__all__ = [
    # Models
    "ComplexOrder",
    "OrderItem",
    "CustomerProfile",
    "Product",
    "AnalyticsEvent",
    "BatchProcessingRequest",
    "ValidationErrorTest",
    # Generators
    "DataGenerator",
    "generate_test_orders",
    "generate_analytics_batch",
    "generate_error_test_data",
]
