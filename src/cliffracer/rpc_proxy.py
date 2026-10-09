"""Dynamic RPC proxy descriptor for inter-service calls.

Binds as a service attribute to dispatch method calls as remote RPC requests
over NATS to `{target_service}.rpc.{method}`.
"""

from __future__ import annotations

import weakref
from collections.abc import AsyncGenerator, Callable, Coroutine
from typing import Any, cast, overload

from cliffracer.core.exceptions import RpcValidationError


class InstanceCache:
    """A per-instance cache keyed by the instance's identity, not its equality or hash.

    A proxy descriptor lives on the class, so one cache serves every instance of a service. Keyed
    on the instance itself (a `WeakKeyDictionary`), two service instances that compare equal share
    an entry, and a service whose class defines `__eq__` without `__hash__`, which includes every
    `@dataclass` service, cannot be a key at all. Here the key is `id(instance)`, and an entry
    is dropped, by a weak-reference callback, when its instance is collected, so an id is never
    looked up after its owner has gone.
    """

    def __init__(self) -> None:
        self._entries: dict[int, tuple[weakref.ref[Any], Any]] = {}

    def __len__(self) -> int:
        return len(self._entries)

    def get_or_make(self, instance: Any, make: Callable[[], Any]) -> Any:
        key = id(instance)
        entry = self._entries.get(key)
        if entry is not None:
            return entry[1]

        def evict(ref: weakref.ref[Any], key: int = key) -> None:
            current = self._entries.get(key)
            if current is not None and current[0] is ref:
                del self._entries[key]

        value = make()
        self._entries[key] = (weakref.ref(instance, evict), value)
        return value


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
        try:
            return await instance.call_rpc(
                self._service_name, self._method_name, namespace=self._namespace, **kwargs
            )
        except RpcValidationError as exc:
            if not any(d.get("type") == "stream_mismatch" for d in exc.details):
                raise
            raise RpcValidationError(
                exc.details,
                f"{self._service_name}.{self._method_name} streams its reply: iterate "
                f"`.{self._method_name}.stream(...)` instead of awaiting it",
            ) from exc

    def stream(self, **kwargs: Any) -> AsyncGenerator[Any]:
        """Call a remote method that streams its reply, for `async for item in ...`.

        Goes through the service's `stream_rpc`, so it is bounded and hooked as that is.
        """
        instance = self._service_instance()
        if instance is None:
            raise RuntimeError("Service instance was garbage collected")
        return cast(
            AsyncGenerator[Any],
            instance.stream_rpc(
                self._service_name, self._method_name, namespace=self._namespace, **kwargs
            ),
        )

    def call_async(self, **kwargs: Any) -> Coroutine[Any, Any, Any]:
        """
        Make a fire-and-forget RPC call (no response expected).

        Publishes to the target's ``{service}.async.{method}`` subject, so the
        callee runs it under its ``max_async_rpc_concurrency`` budget rather
        than the request/reply one. Same subject as ``service.call_async``.

        Args:
            **kwargs: Arguments to pass to the remote method

        Returns:
            A coroutine that publishes the message when awaited. Nothing is
            sent until it is awaited, so a call whose result is dropped sends
            nothing; mypy reports that as ``unused-coroutine``.
        """
        instance = self._service_instance()
        if instance is None:
            raise RuntimeError("Service instance was garbage collected")
        return cast(
            Coroutine[Any, Any, Any],
            instance.call_async(
                self._service_name, self._method_name, namespace=self._namespace, **kwargs
            ),
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
        self._proxies = InstanceCache()  # One ServiceProxy per service instance, by identity
        #: The class attribute this proxy is bound to, set when the class body is built.
        self.attr_name: str | None = None

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

        # Cache the ServiceProxy per service instance, by identity
        proxy: ServiceProxy = self._proxies.get_or_make(
            instance, lambda: ServiceProxy(instance, self.service_name, self._namespace)
        )
        return proxy

    def __set_name__(self, owner: type, name: str) -> None:
        """Remember the attribute this proxy is bound to, so a refused assignment can name it."""
        self.attr_name = name

    def __set__(self, instance: Any, value: Any) -> None:
        """Refuse to give one instance an attribute of its own under the proxy's name.

        Without this the proxy is a non-data descriptor, and an instance attribute of the same
        name wins, so `self.inventory = something` in `__init__` would silently replace the
        proxy for that instance and every call through it would stop reaching the service.
        Replace the proxy on the class to change what a name resolves to.
        """
        raise AttributeError(
            f"{type(instance).__name__}.{self.attr_name or self.service_name} is a proxy to "
            f"{self.service_name!r}, so assigning to it would hide the proxy on this instance. "
            f"Replace it on the class instead."
        )
