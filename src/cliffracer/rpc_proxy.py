"""Dynamic RPC proxy descriptor for inter-service calls.

Binds as a service attribute to dispatch method calls as remote RPC requests
over NATS to `{target_service}.rpc.{method}`.
"""

from __future__ import annotations

import weakref
from typing import Any, overload


class MethodProxy:
    """
    Proxy for a specific method on a remote service.

    When called, it makes an RPC call to the target service's method.
    """

    def __init__(
        self,
        service_instance: Any,
        service_name: str,
        method_name: str,
        namespace: str | None = None,
    ):
        """
        Args:
            service_instance: The service instance that owns this proxy
            service_name: Name of the target service
            method_name: Name of the method to call on target service
            namespace: Optional namespace override for the target service
        """
        self._service_instance = weakref.ref(service_instance)
        self._service_name = service_name
        self._method_name = method_name
        self._namespace = namespace

    async def __call__(self, **kwargs: Any) -> Any:
        """
        Make the RPC call to the target service method.

        Args:
            **kwargs: Arguments to pass to the remote method

        Returns:
            The result from the remote method
        """
        instance = self._service_instance()
        if instance is None:
            raise RuntimeError("Service instance was garbage collected")
        return await instance.call_rpc(
            self._service_name, self._method_name, namespace=self._namespace, **kwargs
        )

    def call_async(self, **kwargs: Any) -> Any:
        """
        Make a fire-and-forget RPC call (no response expected).

        Args:
            **kwargs: Arguments to pass to the remote method
        """
        # This returns a coroutine that can be awaited
        instance = self._service_instance()
        if instance is None:
            raise RuntimeError("Service instance was garbage collected")
        return instance.call_rpc_no_wait(
            self._service_name, self._method_name, namespace=self._namespace, **kwargs
        )


class ServiceProxy:
    """
    Proxy for a remote service.

    Implements __getattr__ to return MethodProxy instances for any method name.
    """

    def __init__(self, service_instance: Any, service_name: str, namespace: str | None = None):
        """
        Args:
            service_instance: The service instance that owns this proxy
            service_name: Name of the target service
            namespace: Optional namespace override for the target service
        """
        self._service_instance = weakref.ref(service_instance)
        self._service_name = service_name
        self._namespace = namespace

    def __getattr__(self, method_name: str) -> MethodProxy:
        """A MethodProxy dispatching calls to ``method_name`` on the remote service."""
        # Avoid infinite recursion for private attributes
        if method_name.startswith("_"):
            raise AttributeError(f"'{type(self).__name__}' object has no attribute '{method_name}'")

        instance = self._service_instance()
        if instance is None:
            raise RuntimeError("Service instance was garbage collected")
        return MethodProxy(instance, self._service_name, method_name, self._namespace)


class RpcProxy:
    """
    Descriptor that provides a proxy to another service.

    Usage:
        class MyService(CliffracerService):
            other_service = RpcProxy("other_service")

            @rpc
            async def my_method(self):
                result = await self.other_service.some_method(param="value")
                return result
    """

    def __init__(self, service_name: str, namespace: str | None = None):
        """
        Args:
            service_name: Name of the target service to proxy
            namespace: Optional namespace override for routing calls to this service
        """
        self.service_name = service_name
        self._namespace = namespace
        self._proxies: weakref.WeakKeyDictionary[Any, ServiceProxy] = (
            weakref.WeakKeyDictionary()
        )  # Cache per instance

    @overload
    def __get__(self, instance: None, owner: type | None = None) -> RpcProxy: ...

    @overload
    def __get__(self, instance: object, owner: type | None = None) -> ServiceProxy: ...

    def __get__(self, instance: Any, owner: type | None = None) -> ServiceProxy | RpcProxy:
        """
        Descriptor protocol implementation.

        Returns a ServiceProxy when accessed from a service instance.

        Args:
            instance: The service instance accessing this descriptor
            owner: The service class

        Returns:
            ServiceProxy for the target service
        """
        if instance is None:
            # Accessed from class, not instance
            return self

        # Cache the ServiceProxy per service instance
        if instance not in self._proxies:
            self._proxies[instance] = ServiceProxy(instance, self.service_name, self._namespace)

        return self._proxies[instance]

    def __set_name__(self, owner: type, name: str) -> None:
        """
        Called when the descriptor is assigned to a class attribute.

        Args:
            owner: The class that owns this descriptor
            name: The name of the attribute
        """
        self.attr_name = name
