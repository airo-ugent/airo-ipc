"""Context: owns the transport resources (iceoryx2 node, Zenoh sessions) of one process.

Most code uses the module-level functions (``airo_ipc.publisher(...)``), which use a default
context per process. Create a :class:`Context` explicitly to control lifetime or sizing::

    with ipc.Context() as ctx:
        pub = ctx.publisher("camera/front", CameraFrame, ipc.Local())
"""

from __future__ import annotations

import os
import threading
import weakref
from collections.abc import Callable
from typing import TYPE_CHECKING, Any, TypeVar

from airo_ipc.defaults import Iox2Sizing
from airo_ipc.endpoints import Client, Publisher, Server, Subscriber
from airo_ipc.errors import ClosedError
from airo_ipc.message import Message
from airo_ipc.qos import Latest, QoS, Queue
from airo_ipc.transport import Local, Network, Transport, validate_topic

if TYPE_CHECKING:
    from airo_ipc.backends.iox2_backend import Iox2Backend
    from airo_ipc.backends.zenoh_backend import ZenohBackend

__all__ = ["Context", "default_context"]

M = TypeVar("M", bound=Message)
Req = TypeVar("Req", bound=Message)
Rep = TypeVar("Rep", bound=Message)


def _check_message_type(what: str, msg_type: object) -> None:
    if not (isinstance(msg_type, type) and issubclass(msg_type, Message)):
        raise TypeError(f"{what} must be a Message subclass, got {msg_type!r}")
    if msg_type._airo_abstract_:
        raise TypeError(msg_type._abstract_error())


def _check_transport(transport: object) -> None:
    if not isinstance(transport, (Local, Network)):
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

    Args:
        iox2: iceoryx2 sizing. Every process of a system must use the same sizing.
    """

    def __init__(self, *, iox2: Iox2Sizing | None = None) -> None:
        self._pid = os.getpid()
        self._iox2_sizing = iox2 or Iox2Sizing()
        self._iox2: Iox2Backend | None = None
        self._zenoh: ZenohBackend | None = None
        self._endpoints: weakref.WeakSet[Any] = weakref.WeakSet()
        self._lock = threading.RLock()
        self._closed = False

    # -- factories ------------------------------------------------------------------------

    def publisher(
        self,
        topic: str,
        msg_type: type[M],
        transport: Transport,
        *,
        latched: bool = False,
    ) -> Publisher[M]:
        """Create a publisher.

        Args:
            topic: Topic name, for example ``"camera/front/rgb"``.
            msg_type: The concrete :class:`~airo_ipc.message.Message` class.
            transport: ``Local()`` or ``Network(...)``.
            latched: Keep the last message for subscribers that connect later.
        """
        self._check_args(topic, transport, ("msg_type", msg_type))
        with self._lock:
            port = self._backend(transport).publisher(
                transport, topic, msg_type, latched
            )
            return self._track(Publisher(port, topic, msg_type, transport, latched))

    def subscriber(
        self,
        topic: str,
        msg_type: type[M],
        transport: Transport,
        *,
        qos: QoS = Latest(),
    ) -> Subscriber[M]:
        """Create a subscriber.

        Args:
            topic: Topic name.
            msg_type: The concrete :class:`~airo_ipc.message.Message` class.
            transport: ``Local()`` or ``Network(...)``.
            qos: ``Latest()`` (default: newest message only) or ``Queue(depth)``.
        """
        self._check_args(topic, transport, ("msg_type", msg_type))
        if not isinstance(qos, (Latest, Queue)):
            raise TypeError(f"qos must be Latest() or Queue(depth), got {qos!r}")
        with self._lock:
            port = self._backend(transport).subscriber(transport, topic, msg_type, qos)
            return self._track(Subscriber(port, topic, msg_type, transport))

    def client(
        self,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
    ) -> Client[Req, Rep]:
        """Create a service client. Call it with ``client.call(request)``."""
        self._check_args(
            topic,
            transport,
            ("request_type", request_type),
            ("response_type", response_type),
        )
        with self._lock:
            backend = self._backend(transport)
            port = backend.client(transport, topic, request_type, response_type)
            return self._track(
                Client(
                    port,
                    topic,
                    request_type,
                    response_type,
                    transport,
                    backend.clients_thread_safe,
                )
            )

    def server(
        self,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
        handler: Callable[[Req], Rep],
        *,
        spin: bool = True,
    ) -> Server[Req, Rep]:
        """Create a service server.

        Args:
            handler: Called with each request; returns the response. If it raises, the client
                gets a :class:`~airo_ipc.errors.ServiceError`.
            spin: True (default): handle requests on a background thread. False: call
                ``server.spin_once(timeout)`` in your own loop.
        """
        self._check_args(
            topic,
            transport,
            ("request_type", request_type),
            ("response_type", response_type),
        )
        if not callable(handler):
            raise TypeError(f"handler must be callable, got {handler!r}")
        with self._lock:
            backend = self._backend(transport)
            port = backend.server(transport, topic, request_type, response_type)
            return self._track(
                Server(
                    port, topic, request_type, response_type, transport, handler, spin
                )
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
        if self._closed:
            raise ClosedError("the context is closed")
        if os.getpid() != self._pid:
            raise RuntimeError(
                f"this Context was created in process {self._pid}; create a new Context in "
                f"process {os.getpid()} (iceoryx2 and Zenoh resources do not survive fork)"
            )
        validate_topic(topic)
        _check_transport(transport)
        for what, msg_type in types:
            _check_message_type(what, msg_type)

    def _track(self, endpoint: Any) -> Any:
        self._endpoints.add(endpoint)
        return endpoint

    def _backend(self, transport: Transport) -> Any:
        """The backend for ``transport``. Both backends have the same factory signatures."""
        return (
            self._iox2_backend()
            if isinstance(transport, Local)
            else self._zenoh_backend()
        )

    def _iox2_backend(self) -> Iox2Backend:
        if self._iox2 is None:
            from airo_ipc.backends.iox2_backend import Iox2Backend

            self._iox2 = Iox2Backend(self._iox2_sizing)
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
