# Timer Examples

This directory contains examples of using Cliffracer's timer functionality, inspired by Nameko's `@timer` decorator.

## Overview

The `@timer` decorator allows you to schedule methods to run at regular intervals, perfect for:

- Health checks
- Metrics collection  
- Cache cleanup
- Data synchronization
- Background processing

## Basic Usage

```python
from cliffracer import NATSService, timer

class MyService(NATSService):
    @timer(interval=30)  # Every 30 seconds
    async def health_check(self):
        await self.check_database_connection()
        
    @timer(interval=60, eager=True)  # Every minute, start immediately  
    async def collect_metrics(self):
        metrics = self.get_performance_data()
        await self.publish_event("metrics.collected", data=metrics)
```

## Timer Options

- `interval`: Time in seconds between executions
- `eager`: If `True`, execute immediately when service starts (default: `False`)
- `max_drift`: Maximum timing drift tolerance in seconds (default: `1.0`)
- `error_backoff`: Delay after error before retry (default: `5.0`)

## Examples

### 1. Basic Timer Service (`timer_service_example.py`)

A comprehensive example showing:
- Health monitoring every 30 seconds
- Metrics collection every minute (eager)
- Data cleanup every 5 minutes
- Data synchronization every 2 minutes
- Connection heartbeat every 10 seconds

Run it:
```bash
cd examples/timer
python timer_service_example.py
```

## Features

### ✅ **Async & Sync Support**
Both async and sync methods work with timers:

```python
@timer(interval=30)
async def async_task(self):
    await some_async_operation()

@timer(interval=60)  
def sync_task(self):
    some_sync_operation()
```

### ✅ **Error Handling**
Timers continue running even if individual executions fail:

```python
@timer(interval=10)
async def robust_task(self):
    try:
        await risky_operation()
    except Exception as e:
        logger.error(f"Task failed: {e}")
        # Timer continues running
```

### ✅ **Performance Monitoring**
Get timer execution statistics:

```python
# Get stats for all timers
stats = service.get_timer_stats()

# Stats include:
# - execution_count
# - error_count  
# - average_execution_time
# - error_rate
```

### ✅ **Service Integration**
Timers integrate seamlessly with Cliffracer services:
- Auto-discovery during service startup
- Automatic start/stop with service lifecycle
- Statistics included in service info

## Best Practices

1. **Choose appropriate intervals**: Don't make timers too frequent
2. **Handle errors gracefully**: Wrap timer logic in try/catch
3. **Use eager=True sparingly**: Only for initialization tasks
4. **Monitor performance**: Check timer statistics regularly
5. **Keep timer methods focused**: Each timer should have a single responsibility

## Performance Considerations

- Timers run concurrently with each other
- Long-running timer methods don't block other timers
- Automatic drift correction prevents timing issues
- Built-in error recovery with configurable backoff

## Integration with Other Features

Timers work great with:
- **PerformanceMetrics**: Automatic metrics collection
- **Database operations**: Scheduled cleanup and sync
- **WebSocket broadcasts**: Periodic updates to clients
- **RPC calls**: Health checks and service communication

## Monitoring

Monitor your timers with:

```python
# Service-level timer stats
timer_stats = service.get_timer_stats()

# Individual timer performance
for timer_info in timer_stats["timers"]:
    print(f"{timer_info['method_name']}: {timer_info['execution_count']} executions")
```

The timer implementation provides comprehensive statistics including execution counts, error rates, timing information, and performance metrics.