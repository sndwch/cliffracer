"""cliffracer-metrics: dispatch timing, batching and connection pooling.

`MetricsExtension` is registered on a service and runs on every dispatch.

`BatchProcessor`, `OptimizedNATSConnection`, and `PerformanceMetrics` are
library classes exported for direct application use.
"""

from .batch_processor import BatchProcessor
from .connection_pool import OptimizedNATSConnection
from .extension import MetricsExtension
from .metrics import PerformanceMetrics
from .pool_extension import PoolExtension

__all__ = [
    "MetricsExtension",
    "PoolExtension",
    "BatchProcessor",
    "OptimizedNATSConnection",
    "PerformanceMetrics",
]
