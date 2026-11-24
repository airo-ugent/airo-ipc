"""Context: owns the transport resources (iceoryx2 node, Zenoh sessions) of one process.

Most code uses the module-level functions (``airo_ipc.publisher(...)``), which use a default
context per process. Create a :class:`Context` explicitly to control the lifetime::

    with ipc.Context() as ctx:
        pub = ctx.publisher("camera/front", CameraFrame, ipc.Local())
"""

from __future__ import annotations

import os
import threading
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from airo_ipc.backends.base import MAX_QUEUE_DEPTH
from airo_ipc.endpoints import Client, Publisher, Server, Subscriber
from airo_ipc.errors import ClosedError
from airo_ipc.message import Message
from airo_ipc.transport import Local, Network, Transport, validate_topic

if TYPE_CHECKING:
    from airo_ipc.backends.iox2_backend import Iox2Backend
    from airo_ipc.backends.zenoh_backend import ZenohBackend

__all__ = ["Context", "default_context"]

M = TypeVar("M", bound=Message)
Req = TypeVar("Req", bound=Message)
Rep = TypeVar("Rep", bound=Message)
E = TypeVar("E")


def _check_message_type(what: str, msg_type: object) -> None:
    if not (isinstance(msg_type, type) and issubclass(msg_type, Message)):
        raise TypeError(f"{what} must be a Message subclass, got {msg_type!r}")
    msg_type._require_concrete()


def _check_transport(transport: object) -> None:
    if not isinstance(transport, Transport):
        hint = (
            " (pass an instance: Local(), not Local)"
            if transport in (Local, Network)
            else ""
        )
        raise TypeError(
            f"transport must be Local() or Network(...), got {transport!r}{hint}"
        )


class Context:
    """Transport resources of one process, and the factories that use them.

    A context belongs to the process that created it. Create a new one in a child process
    (``multiprocessing`` with the "spawn" start method, or after ``fork``).
    """

    def __init__(self) -> None:
        self._pid = os.getpid()
        self._iox2: Iox2Backend | None = None
        self._zenoh: ZenohBackend | None = None
        self._endpoints: weakref.WeakSet[Any] = weakref.WeakSet()
        self._lock = threading.Lock()
        self._closed = False

    # -- factories ------------------------------------------------------------------------

    def publisher(
        self,
        topic: str,
        msg_type: type[M],
        transport: Transport,
    ) -> Publisher[M]:
        """Create a publisher.

        Args:
            topic: Topic name, for example ``"camera/front/rgb"``.
            msg_type: The concrete :class:`~airo_ipc.message.Message` class.
            transport: ``Local()`` or ``Network(...)``.
        """
        self._check_args(topic, transport, ("msg_type", msg_type))

        def make() -> Publisher[M]:
            if isinstance(transport, Local):
                port = self._iox2_backend().publisher(topic, msg_type)
            else:
                port = self._zenoh_backend().publisher(transport, topic, msg_type)
            return Publisher(port, topic, msg_type, transport)

        return self._create(make)

    def subscriber(
        self,
        topic: str,
        msg_type: type[M],
        transport: Transport,
        *,
        depth: int = 1,
    ) -> Subscriber[M]:
        """Create a subscriber.

        Args:
            topic: Topic name.
            msg_type: The concrete :class:`~airo_ipc.message.Message` class.
            transport: ``Local()`` or ``Network(...)``.
            depth: Unread messages to keep, oldest first; when full, the oldest is dropped (the
                publisher never blocks). 1 (default): only the newest message, right for sensor
                streams. At most :data:`~airo_ipc.backends.base.MAX_QUEUE_DEPTH`.
        """
        self._check_args(topic, transport, ("msg_type", msg_type))
        if isinstance(depth, bool) or not isinstance(depth, int):
            raise TypeError(f"depth must be an int, got {depth!r}")
        if not 1 <= depth <= MAX_QUEUE_DEPTH:
            raise ValueError(
                f"depth must be between 1 and {MAX_QUEUE_DEPTH}, got {depth}"
            )

        def make() -> Subscriber[M]:
            if isinstance(transport, Local):
                port = self._iox2_backend().subscriber(topic, msg_type, depth)
            else:
                port = self._zenoh_backend().subscriber(
                    transport, topic, msg_type, depth
                )
            return Subscriber(port, topic, msg_type, transport)

        return self._create(make)

    def client(
        self,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
    ) -> Client[Req, Rep]:
        """Create a service client. Call it with ``client.call(request)``.

        Services use Zenoh on both transports; on ``Local()`` it stays on this host.
        """
        self._check_args(
            topic,
            transport,
            ("request_type", request_type),
            ("response_type", response_type),
        )
        return self._create(
            lambda: Client(
                self._zenoh_backend().client(
                    transport, topic, request_type, response_type
                ),
                topic,
                request_type,
                response_type,
                transport,
            ),
        )

    def server(
        self,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
        handler: Callable[[Req], Rep],
    ) -> Server[Req, Rep]:
        """Create a service server. Call ``server.spin_once(timeout)`` in your own loop.

        Args:
            handler: Called by ``spin_once()`` with each request; returns the response. If it
                raises, the client gets a :class:`~airo_ipc.errors.ServiceError`.
        """
        self._check_args(
            topic,
            transport,
            ("request_type", request_type),
            ("response_type", response_type),
        )
        if not callable(handler):
            raise TypeError(f"handler must be callable, got {handler!r}")
        return self._create(
            lambda: Server(
                self._zenoh_backend().server(
                    transport, topic, request_type, response_type
                ),
                topic,
                request_type,
                response_type,
                transport,
                handler,
            ),
        )

    # -- lifetime -------------------------------------------------------------------------

    @property
    def closed(self) -> bool:
        return self._closed

    def close(self) -> None:
        """Close every endpoint of this context, then the transport resources."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            endpoints = list(self._endpoints)
        for endpoint in endpoints:
            try:
                endpoint.close()
            except Exception:  # pragma: no cover - keep closing the rest
                pass
        if self._zenoh is not None:
            self._zenoh.close()
            self._zenoh = None
        if self._iox2 is not None:
            self._iox2.close()
            self._iox2 = None

    def __enter__(self) -> Context:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"<Context pid={self._pid} {'closed' if self._closed else 'open'}>"

    # -- internals ------------------------------------------------------------------------

    def _check_args(
        self, topic: str, transport: object, *types: tuple[str, object]
    ) -> None:
        if os.getpid() != self._pid:
            raise RuntimeError(
                f"this Context was created in process {self._pid}; create a new Context in "
                f"process {os.getpid()} (iceoryx2 and Zenoh resources do not survive fork)"
            )
        validate_topic(topic)
        _check_transport(transport)
        for what, msg_type in types:
            _check_message_type(what, msg_type)

    def _create(self, make: Callable[[], E]) -> E:
        """Build an endpoint with ``make()`` and track it, so that ``close()`` closes it.

        Pub/sub on ``Local()`` uses iceoryx2; everything else (``Network()``, and services on
        both transports) uses Zenoh.
        """
        with self._lock:
            if self._closed:
                raise ClosedError("the context is closed")
            endpoint = make()
            self._endpoints.add(endpoint)
            return endpoint

    def _iox2_backend(self) -> Iox2Backend:
        if self._iox2 is None:
            from airo_ipc.backends.iox2_backend import Iox2Backend

            self._iox2 = Iox2Backend()
        return self._iox2

    def _zenoh_backend(self) -> ZenohBackend:
        if self._zenoh is None:
            from airo_ipc.backends.zenoh_backend import ZenohBackend

            self._zenoh = ZenohBackend()
        return self._zenoh


_default: Context | None = None
_default_lock = threading.Lock()


def default_context() -> Context:
    """The context used by the module-level functions; one per process, created on first use."""
    global _default
    with _default_lock:
        if _default is None or _default._pid != os.getpid() or _default.closed:
            _default = Context()
        return _default
