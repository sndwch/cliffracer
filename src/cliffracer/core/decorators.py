"""
Consolidated decorators for Cliffracer services

This module provides all service decorators in one place with consistent
patterns and composable functionality.
"""

import inspect
from collections.abc import Callable

from pydantic import BaseModel


def rpc(func: Callable) -> Callable:
    """
    Decorator to mark a method as an RPC handler.

    The method will be exposed as {service_name}.rpc.{method_name}
    """
    func._cliffracer_rpc = True
    return func


def async_rpc(func: Callable) -> Callable:
    """
    Decorator to mark a method as an async RPC handler.

    Same as @rpc but emphasizes async nature for clarity.
    """
    func._cliffracer_rpc = True
    func._cliffracer_async_rpc = True
    return func


def validated_rpc(schema: type[BaseModel]) -> Callable:
    """
    Decorator to mark a method as a validated RPC handler.

    Args:
        schema: Pydantic model class for request validation

    Example:
        @validated_rpc(UserCreateRequest)
        async def create_user(self, request: UserCreateRequest):
            return {"user_id": f"user_{request.username}"}
    """

    def decorator(func: Callable) -> Callable:
        func._cliffracer_rpc = True
        func._cliffracer_validated_rpc = schema
        return func

    return decorator


def listener(pattern: str) -> Callable:
    """
    Decorator to mark a method as an event listener.

    Args:
        pattern: NATS subject pattern to listen for (supports wildcards)

    Example:
        @listener("user.events.*")
        async def handle_user_event(self, subject: str, **data):
            print(f"User event: {subject}")
    """

    def decorator(func: Callable) -> Callable:
        if not hasattr(func, "_cliffracer_events"):
            func._cliffracer_events = []
        func._cliffracer_events.append(pattern)
        return func

    return decorator


def broadcast(pattern: str) -> Callable:
    """
    Decorator to mark a method as a broadcast handler.

    Args:
        pattern: Message pattern to handle

    Example:
        @broadcast("system.alerts")
        async def handle_alert(self, **data):
            await self.broadcast_to_websockets(data)
    """

    def decorator(func: Callable) -> Callable:
        func._cliffracer_broadcast = pattern
        return func

    return decorator


def websocket_handler(path: str = "/ws") -> Callable:
    """
    Decorator to mark a method as a WebSocket handler.

    Args:
        path: WebSocket endpoint path

    Example:
        @websocket_handler("/notifications")
        async def handle_notifications(self, websocket: WebSocket):
            while True:
                data = await websocket.receive_text()
                await websocket.send_text(f"Echo: {data}")
    """

    def decorator(func: Callable) -> Callable:
        func._cliffracer_websocket = path
        return func

    return decorator


def timer(interval: float, eager: bool = False, **kwargs) -> Callable:
    """
    Decorator for creating timer-triggered methods.

    Args:
        interval: Time in seconds between executions
        eager: If True, execute immediately on service start
        **kwargs: Additional timer configuration options

    Example:
        @timer(interval=30)
        async def health_check(self):
            await self.check_database_connection()

        @timer(interval=60, eager=True)
        async def cleanup_cache(self):
            await self.remove_expired_entries()
    """

    def decorator(func: Callable) -> Callable:
        from .timer import Timer

        timer_instance = Timer(interval=interval, eager=eager, **kwargs)
        return timer_instance(func)

    return decorator


def http_endpoint(method: str, path: str, **kwargs) -> Callable:
    """
    Generic HTTP endpoint decorator.

    Args:
        method: HTTP method (GET, POST, PUT, DELETE, etc.)
        path: URL path
        **kwargs: Additional FastAPI decorator arguments
    """

    def decorator(func: Callable) -> Callable:
        func._cliffracer_http_endpoint = {"method": method.upper(), "path": path, "kwargs": kwargs}
        return func

    return decorator


def get(path: str, **kwargs) -> Callable:
    """Decorator for GET endpoints"""
    return http_endpoint("GET", path, **kwargs)


def post(path: str, **kwargs) -> Callable:
    """Decorator for POST endpoints"""
    return http_endpoint("POST", path, **kwargs)


def put(path: str, **kwargs) -> Callable:
    """Decorator for PUT endpoints"""
    return http_endpoint("PUT", path, **kwargs)


def delete(path: str, **kwargs) -> Callable:
    """Decorator for DELETE endpoints"""
    return http_endpoint("DELETE", path, **kwargs)


def monitor_performance(
    track_latency: bool = True, track_errors: bool = True, custom_metrics: list[str] | None = None
) -> Callable:
    """
    Decorator to monitor method performance.

    Args:
        track_latency: Whether to track execution time
        track_errors: Whether to track error rates
        custom_metrics: List of custom metric names to collect
    """

    def decorator(func: Callable) -> Callable:
        async def async_wrapper(self, *args, **kwargs):
            if not hasattr(self, "_metrics") or not self._metrics:
                # No metrics available, just execute
                return await func(self, *args, **kwargs)

            import time

            start_time = time.perf_counter()
            success = False

            try:
                result = await func(self, *args, **kwargs)
                success = True
                return result
            except Exception:
                if track_errors:
                    self._metrics.increment_counter(f"{func.__name__}_errors")
                raise
            finally:
                if track_latency:
                    end_time = time.perf_counter()
                    latency_ms = (end_time - start_time) * 1000
                    self._metrics.record_latency(latency_ms, success)
                    self._metrics.record_custom_metric(f"{func.__name__}_duration_ms", latency_ms)

                self._metrics.increment_counter(f"{func.__name__}_calls")

        def sync_wrapper(self, *args, **kwargs):
            if not hasattr(self, "_metrics") or not self._metrics:
                return func(self, *args, **kwargs)

            import time

            start_time = time.perf_counter()

            try:
                result = func(self, *args, **kwargs)
                return result
            except Exception:
                if track_errors:
                    self._metrics.increment_counter(f"{func.__name__}_errors")
                raise
            finally:
                if track_latency:
                    end_time = time.perf_counter()
                    latency_ms = (end_time - start_time) * 1000
                    self._metrics.record_custom_metric(f"{func.__name__}_duration_ms", latency_ms)

                self._metrics.increment_counter(f"{func.__name__}_calls")

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def retry(
    max_attempts: int = 3,
    backoff_delay: float = 1.0,
    exceptions: tuple[type[Exception], ...] = (Exception,),
) -> Callable:
    """
    Decorator to retry failed method calls.

    Args:
        max_attempts: Maximum number of retry attempts
        backoff_delay: Delay between retries (in seconds)
        exceptions: Tuple of exception types to retry on
    """

    def decorator(func: Callable) -> Callable:
        async def async_wrapper(self, *args, **kwargs):
            import asyncio

            from loguru import logger

            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return await func(self, *args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        logger.warning(f"Attempt {attempt + 1} failed for {func.__name__}: {e}")
                        await asyncio.sleep(backoff_delay * (attempt + 1))
                    else:
                        logger.error(f"All {max_attempts} attempts failed for {func.__name__}")

            raise last_exception

        def sync_wrapper(self, *args, **kwargs):
            import time

            from loguru import logger

            last_exception = None

            for attempt in range(max_attempts):
                try:
                    return func(self, *args, **kwargs)
                except exceptions as e:
                    last_exception = e
                    if attempt < max_attempts - 1:
                        logger.warning(f"Attempt {attempt + 1} failed for {func.__name__}: {e}")
                        time.sleep(backoff_delay * (attempt + 1))
                    else:
                        logger.error(f"All {max_attempts} attempts failed for {func.__name__}")

            raise last_exception

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


def cache_result(ttl_seconds: int = 60) -> Callable:
    """
    Decorator to cache method results.

    Args:
        ttl_seconds: Time to live for cached results
    """

    def decorator(func: Callable) -> Callable:
        cache = {}

        def _get_cache_key(*args, **kwargs):
            return hash(str(args) + str(sorted(kwargs.items())))

        async def async_wrapper(self, *args, **kwargs):
            import time

            cache_key = _get_cache_key(*args, **kwargs)
            current_time = time.time()

            # Check cache
            if cache_key in cache:
                result, timestamp = cache[cache_key]
                if current_time - timestamp < ttl_seconds:
                    return result
                else:
                    del cache[cache_key]

            # Execute and cache
            result = await func(self, *args, **kwargs)
            cache[cache_key] = (result, current_time)
            return result

        def sync_wrapper(self, *args, **kwargs):
            import time

            cache_key = _get_cache_key(*args, **kwargs)
            current_time = time.time()

            # Check cache
            if cache_key in cache:
                result, timestamp = cache[cache_key]
                if current_time - timestamp < ttl_seconds:
                    return result
                else:
                    del cache[cache_key]

            # Execute and cache
            result = func(self, *args, **kwargs)
            cache[cache_key] = (result, current_time)
            return result

        if inspect.iscoroutinefunction(func):
            return async_wrapper
        else:
            return sync_wrapper

    return decorator


# Composition helpers
def compose_decorators(*decorators) -> Callable:
    """
    Compose multiple decorators into one.

    Example:
        @compose_decorators(
            timer(interval=30),
            monitor_performance(),
            retry(max_attempts=3)
        )
        async def robust_task(self):
            await self.do_something()
    """

    def decorator(func: Callable) -> Callable:
        for dec in reversed(decorators):
            func = dec(func)
        return func

    return decorator


# Convenience decorator combinations
def robust_rpc(
    schema: type[BaseModel] | None = None, max_attempts: int = 3, monitor: bool = True
) -> Callable:
    """
    Decorator that combines RPC, validation, retry, and monitoring.

    Args:
        schema: Optional Pydantic schema for validation
        max_attempts: Number of retry attempts
        monitor: Whether to monitor performance
    """
    decorators = []

    if schema:
        decorators.append(validated_rpc(schema))
    else:
        decorators.append(rpc)

    decorators.append(retry(max_attempts=max_attempts))

    if monitor:
        decorators.append(monitor_performance())

    return compose_decorators(*decorators)


def scheduled_task(
    interval: float, eager: bool = False, monitor: bool = True, max_attempts: int = 2
) -> Callable:
    """
    Decorator for robust scheduled tasks.

    Combines timer, monitoring, and retry functionality.
    """
    decorators = [
        timer(interval=interval, eager=eager),
        retry(max_attempts=max_attempts, exceptions=(Exception,)),
    ]

    if monitor:
        decorators.append(monitor_performance())

    return compose_decorators(*decorators)
