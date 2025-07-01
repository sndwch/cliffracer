"""
Gevent-based ServiceRunner for Cliffracer
Inspired by Nameko's ServiceRunner pattern
"""

import gevent
import gevent.monkey
from gevent.pool import Pool
from gevent.queue import Queue
import signal
import json
from typing import Optional, Dict, Any, List
from dataclasses import dataclass

from loguru import logger
from ..core.consolidated_service import CliffracerService
from ..core.service_config import ServiceConfig


@dataclass
class GeventServiceConfig:
    """Configuration for gevent-based service runner"""
    
    max_workers: int = 10
    nats_url: str = "nats://localhost:4222"
    pool_size: int = 20
    spawn_timeout: float = 30.0
    enable_monkey_patching: bool = True


class GeventServiceRunner:
    """
    Gevent-based service runner that provides:
    - Concurrent request handling via greenthreads
    - Configurable worker pools
    - Nameko-style architecture
    """
    
    def __init__(self, service_class: type, config: GeventServiceConfig):
        self.service_class = service_class
        self.config = config
        self.workers: List[CliffracerService] = []
        self.worker_pool: Optional[Pool] = None
        self.running = False
        
        # Apply monkey patching if enabled
        if config.enable_monkey_patching:
            gevent.monkey.patch_all()
            logger.info("Applied gevent monkey patching")
    
    def _create_service_instance(self) -> CliffracerService:
        """Create a new service instance with config"""
        service_config = ServiceConfig(
            name=f"{self.service_class.__name__}",
            nats_url=self.config.nats_url
        )
        return self.service_class(service_config)
    
    def _spawn_worker(self, worker_id: int):
        """Spawn a single worker greenthread"""
        try:
            service = self._create_service_instance()
            service.worker_id = worker_id
            self.workers.append(service)
            
            logger.info(f"Starting worker {worker_id} for {self.service_class.__name__}")
            
            # Run the service (this will block the greenthread)
            gevent.spawn(service.run).join()
            
        except Exception as e:
            logger.error(f"Worker {worker_id} failed: {e}")
            raise
    
    def start(self):
        """Start the service runner with worker pool"""
        if self.running:
            logger.warning("Service runner already running")
            return
        
        self.running = True
        logger.info(f"Starting {self.config.max_workers} workers for {self.service_class.__name__}")
        
        # Create worker pool
        self.worker_pool = Pool(self.config.max_workers)
        
        # Spawn workers
        for worker_id in range(self.config.max_workers):
            self.worker_pool.spawn(self._spawn_worker, worker_id)
        
        # Setup signal handlers
        gevent.signal(signal.SIGTERM, self.stop)
        gevent.signal(signal.SIGINT, self.stop)
        
        logger.info(f"Service runner started with {self.config.max_workers} workers")
        
        try:
            # Wait for all workers
            self.worker_pool.join()
        except KeyboardInterrupt:
            logger.info("Received interrupt signal")
            self.stop()
    
    def stop(self):
        """Gracefully stop all workers"""
        if not self.running:
            return
        
        logger.info("Stopping service runner...")
        self.running = False
        
        # Stop all service instances
        for service in self.workers:
            try:
                # Services should implement graceful shutdown
                if hasattr(service, 'stop'):
                    service.stop()
            except Exception as e:
                logger.error(f"Error stopping service: {e}")
        
        # Kill worker pool
        if self.worker_pool:
            self.worker_pool.kill()
        
        logger.info("Service runner stopped")


class GeventRPCHandler:
    """
    Gevent-compatible RPC handler that processes requests concurrently
    """
    
    def __init__(self, service: CliffracerService):
        self.service = service
        self.request_queue = Queue()
        self.worker_pool = Pool(10)  # Configurable
    
    async def handle_rpc_request(self, msg):
        """Handle RPC request in gevent greenthread"""
        # Spawn greenthread for this request
        greenthread = gevent.spawn(self._process_rpc_request, msg)
        
        # Don't await - let it run concurrently
        # This allows multiple requests to be processed simultaneously
        return greenthread
    
    def _process_rpc_request(self, msg):
        """Process RPC request synchronously in greenthread"""
        subject = msg.subject
        handler_name = subject.split(".")[-1]
        
        if handler_name not in self.service._rpc_handlers:
            error_response = {
                "error": f"Unknown method: {handler_name}",
                "worker_id": getattr(self.service, 'worker_id', 'unknown')
            }
            msg.respond(json.dumps(error_response).encode())
            return
        
        handler = self.service._rpc_handlers[handler_name]
        
        try:
            # Parse request data
            data = json.loads(msg.data.decode()) if msg.data else {}
            
            # Call handler (can be sync or async)
            if hasattr(handler, '__call__'):
                result = handler(self.service, **data)
            else:
                result = handler(**data)
            
            # Send response
            response = {
                "result": result,
                "worker_id": getattr(self.service, 'worker_id', 'unknown')
            }
            msg.respond(json.dumps(response).encode())
            
        except Exception as e:
            logger.error(f"RPC handler error: {e}")
            error_response = {
                "error": str(e),
                "worker_id": getattr(self.service, 'worker_id', 'unknown')
            }
            msg.respond(json.dumps(error_response).encode())


# Example usage
def run_service_with_gevent(service_class: type, **config_kwargs):
    """Convenience function to run a service with gevent"""
    
    config = GeventServiceConfig(**config_kwargs)
    runner = GeventServiceRunner(service_class, config)
    
    try:
        runner.start()
    except KeyboardInterrupt:
        logger.info("Shutting down service...")
        runner.stop()


# Example service that benefits from gevent
class ExampleCPUBoundService(CliffracerService):
    """Example service with CPU-bound operations"""
    
    @property
    def rpc(self):
        """RPC decorator that works with gevent"""
        def decorator(func):
            # Register handler normally
            self._rpc_handlers[func.__name__] = func
            return func
        return decorator
    
    @rpc
    def cpu_intensive_task(self, iterations: int = 1000000) -> dict:
        """CPU-bound task that would block asyncio"""
        result = 0
        for i in range(iterations):
            result += i * i
        
        return {
            "result": result,
            "iterations": iterations,
            "worker_id": getattr(self, 'worker_id', 'unknown')
        }
    
    @rpc  
    def blocking_io_task(self, url: str) -> dict:
        """Simulated blocking I/O (gevent will handle this)"""
        import requests  # This will be monkey-patched by gevent
        
        response = requests.get(url, timeout=5)
        return {
            "status_code": response.status_code,
            "content_length": len(response.content),
            "worker_id": getattr(self, 'worker_id', 'unknown')
        }


if __name__ == "__main__":
    # Example usage
    run_service_with_gevent(
        ExampleCPUBoundService,
        max_workers=5,
        nats_url="nats://localhost:4222"
    )