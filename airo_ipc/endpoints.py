"""Public endpoint classes: Publisher, Subscriber, Client and Server.

Create them with the factories in :mod:`airo_ipc` (or on a :class:`~airo_ipc.context.Context`);
do not instantiate them directly. They behave the same on ``Local()`` and ``Network(...)``.

Threads: airo-ipc starts no threads of its own. Call ``receive()`` and ``server.spin_once()`` in
your own loop, and use each endpoint from one thread. ``close()`` (and ``Context.close()``) may be
called from any thread: a blocked ``receive()`` or ``spin_once()`` then stops within
:data:`~airo_ipc.backends.base.WAIT_SLICE_SECONDS`.
"""

from __future__ import annotations

import itertools
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Generic, TypeVar

from airo_ipc.backends.base import (
    ClientPort,
    PublisherPort,
    ServerPort,
    SubscriberPort,
    check_timeout,
    wait_slices,
)
from airo_ipc.errors import ClosedError
from airo_ipc.message import Message
from airo_ipc.transport import Transport

__all__ = ["Publisher", "Subscriber", "Client", "Server"]

logger = logging.getLogger("airo_ipc")

SERVER_POLL_SECONDS = 0.01  # how often wait_for_server() checks for a server

M = TypeVar("M", bound=Message)
Req = TypeVar("Req", bound=Message)
Rep = TypeVar("Rep", bound=Message)


def _check_type(kind: str, expected: type[Message], msg: object) -> None:
    if type(msg) is not expected:
        raise TypeError(
            f"{kind} expects a {expected.__qualname__} message, got {type(msg).__qualname__}"
        )


class _Endpoint:
    _port: Any

    def __init__(self, topic: str, transport: Transport) -> None:
        self._topic = topic
        self._transport = transport
        self._closed = False
        # Held for every port call, so that close() from another thread never frees a port
        # that is in use.
        self._lock = threading.Lock()

    @property
    def topic(self) -> str:
        return self._topic

    @property
    def transport(self) -> Transport:
        return self._transport

    @property
    def closed(self) -> bool:
        return self._closed

    def _check_open(self) -> None:
        if self._closed:
            raise ClosedError(f"{type(self).__name__} for '{self._topic}' is closed")

    def _if_open(self, call: Callable[..., object], *args: Any) -> None:
        """Run the port call ``call(*args)`` unless the endpoint is closed.

        For cleanup calls: a closed port has already freed its loans and messages.
        """
        with self._lock:
            if not self._closed:
                call(*args)

    def close(self) -> None:
        """Close the endpoint. Safe to call from any thread, and more than once."""
        with self._lock:
            if not self._closed:
                self._closed = True
                self._port.close()

    def __enter__(self: Any) -> Any:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<{type(self).__name__} '{self._topic}' {self._transport} {state}>"


class Publisher(_Endpoint, Generic[M]):
    """Sends messages of one type on one topic."""

    _port: PublisherPort

    def __init__(
        self,
        port: PublisherPort,
        topic: str,
        msg_type: type[M],
        transport: Transport,
    ) -> None:
        super().__init__(topic, transport)
        self._port = port
        self._msg_type = msg_type
        self._seq = itertools.count(1)

    @property
    def msg_type(self) -> type[M]:
        return self._msg_type

    @property
    def has_subscribers(self) -> bool:
        """True if at least one subscriber is connected."""
        with self._lock:
            self._check_open()
            return self._port.has_subscribers

    def publish(self, msg: M) -> None:
        """Send a copy of ``msg``. Writes the sequence number and time into ``msg.meta``."""
        _check_type("publish()", self._msg_type, msg)
        with self._lock:
            self._check_open()
            self._stamp(msg)
            self._port.send_copy(msg)

    @contextmanager
    def loan(self) -> Iterator[M]:
        """Write a message in place and send it when the ``with`` block ends.

        On ``Local()`` the message lives in shared memory: writing ``frame.rgb[:] = image`` is the
        only copy. The contents at the start of the block are unspecified, so write every field.
        If the block raises, nothing is sent. After the block, the message and its numpy views
        are read-only and field access raises :class:`~airo_ipc.errors.LoanReleasedError`.

        Example::

            with pub.loan() as frame:
                frame.rgb[:] = image
                frame.stamp = time.time()
        """
        with self._lock:
            self._check_open()
            msg, token = self._port.loan()
        msg._airo_begin_loan(readonly=False)
        try:
            yield msg  # type: ignore[misc]
        except BaseException:
            msg._airo_end_loan()
            self._if_open(self._port.discard, token)
            raise
        msg._airo_end_loan()
        with self._lock:
            self._check_open()
            self._stamp(msg)
            self._port.send(token)

    def _stamp(self, msg: Message) -> None:
        """Write the header. Call with ``self._lock`` held, so sequence numbers stay in order."""
        msg._airo_set_meta(next(self._seq), time.time_ns())


class Subscriber(_Endpoint, Generic[M]):
    """Receives messages of one type on one topic."""

    _port: SubscriberPort

    def __init__(
        self, port: SubscriberPort, topic: str, msg_type: type[M], transport: Transport
    ) -> None:
        super().__init__(topic, transport)
        self._port = port
        self._msg_type = msg_type
        self._warned_escape = False

    @property
    def msg_type(self) -> type[M]:
        return self._msg_type

    @property
    def has_publishers(self) -> bool:
        """True if at least one publisher is known for this topic."""
        with self._lock:
            self._check_open()
            return self._port.has_publishers

    def receive(self, timeout: float | None = 0.0) -> M | None:
        """Return the next message as a copy that you own, or None.

        Args:
            timeout: Seconds to wait for a message. ``0`` (default) does not wait; ``None`` waits
                until a message arrives.
        """
        item = self._take(timeout)
        if item is None:
            return None
        msg, holder = item
        if holder is None:  # the message is already a copy that the caller owns
            return msg  # type: ignore[return-value]
        try:
            return msg.copy()  # type: ignore[return-value]
        finally:
            self._if_open(self._port.release, holder)

    @contextmanager
    def receive_loan(self, timeout: float | None = 0.0) -> Iterator[M | None]:
        """Read the next message in place, without a copy; yield None if there is none.

        On ``Local()`` the message is a read-only view into shared memory. The publisher cannot
        reuse that memory while the ``with`` block runs, so the data cannot change under you.
        Copy what you need before the block ends; afterwards field access raises
        :class:`~airo_ipc.errors.LoanReleasedError`. A numpy view that you keep after the block
        keeps the shared memory borrowed until it is garbage-collected.

        Example::

            with sub.receive_loan(timeout=1.0) as frame:
                if frame is not None:
                    process(frame.rgb)
        """
        item = self._take(timeout)
        if item is None:
            yield None
            return
        msg, holder = item
        msg._airo_begin_loan(readonly=True)
        try:
            yield msg  # type: ignore[misc]
        finally:
            escaped = msg._airo_end_loan()
            if not escaped and holder is not None:
                self._if_open(self._port.release, holder)
            elif not self._warned_escape:
                self._warned_escape = True
                logger.warning(
                    "A numpy view from receive_loan() on '%s' outlived its `with` block. The "
                    "shared memory stays borrowed until the view is garbage-collected; copy the "
                    "data inside the block instead.",
                    self._topic,
                )

    def _take(self, timeout: float | None) -> tuple[Message, Any] | None:
        for wait in wait_slices(timeout):
            with self._lock:
                self._check_open()
                item = self._port.take()
                if item is not None:
                    return item
                if wait > 0:
                    self._port.wait(wait)
        return None


class Client(_Endpoint, Generic[Req, Rep]):
    """Calls a service: sends a request and waits for the response."""

    _port: ClientPort

    def __init__(
        self,
        port: ClientPort,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
    ) -> None:
        super().__init__(topic, transport)
        self._port = port
        self._request_type = request_type
        self._response_type = response_type

    @property
    def request_type(self) -> type[Req]:
        return self._request_type

    @property
    def response_type(self) -> type[Rep]:
        return self._response_type

    @property
    def has_server(self) -> bool:
        """True if a server for this service is available."""
        self._check_open()
        return self._port.has_server

    def wait_for_server(self, timeout: float | None = None) -> bool:
        """Block until a server is available. Return False if ``timeout`` seconds pass first."""
        for wait in wait_slices(timeout):
            if self.has_server:
                return True
            time.sleep(min(wait, SERVER_POLL_SECONDS))
        return False

    def call(self, request: Req, timeout: float | None = 5.0) -> Rep:
        """Send ``request`` and return the response.

        Raises:
            NoServerError: no server is available (raised at once, without waiting).
            TimeoutError: the server did not reply within ``timeout`` seconds.
            ServiceError: the server's handler raised an exception.
        """
        self._check_open()
        _check_type("call()", self._request_type, request)
        check_timeout(timeout)
        # Zenoh ports are thread-safe, so close() from another thread needs no lock here.
        response = self._port.call(request, timeout)
        _check_type("the server's response", self._response_type, response)
        return response  # type: ignore[return-value]


class Server(_Endpoint, Generic[Req, Rep]):
    """Serves a service: :meth:`spin_once` calls ``handler(request)`` and sends the result back."""

    _port: ServerPort

    def __init__(
        self,
        port: ServerPort,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
        handler: Callable[[Req], Rep],
    ) -> None:
        super().__init__(topic, transport)
        self._port = port
        self._request_type = request_type
        self._response_type = response_type
        self._handler = handler

    @property
    def request_type(self) -> type[Req]:
        return self._request_type

    @property
    def response_type(self) -> type[Rep]:
        return self._response_type

    def spin_once(self, timeout: float = 0.0) -> bool:
        """Handle at most one request; wait up to ``timeout`` seconds for it.

        Call it in your own loop, for example ``while running: server.spin_once(0.1)``. Return
        True if a request was handled; False if none came in time or the server was closed.
        """
        self._check_open()
        for wait in wait_slices(max(0.0, timeout)):
            if self._closed:
                return False
            item = self._port.receive(wait)
            if item is not None:
                break
        else:
            return False
        request, token = item
        try:
            response = self._handler(request)  # type: ignore[arg-type]
            _check_type(
                f"the handler of '{self._topic}'", self._response_type, response
            )
        except Exception as e:
            logger.exception("handler of service '%s' raised", self._topic)
            self._port.reply_error(token, e)
            return True
        self._port.reply(token, response)
        return True
