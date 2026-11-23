"""Public endpoint classes: Publisher, Subscriber, Client and Server.

Create them with the factories in :mod:`airo_ipc` (or on a :class:`~airo_ipc.context.Context`);
do not instantiate them directly. They behave the same on ``Local()`` and ``Network(...)``.
"""

from __future__ import annotations

import concurrent.futures
import itertools
import logging
import threading
import time
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from typing import Any, Generic, TypeVar

from airo_ipc.backends.base import ClientPort, PublisherPort, ServerPort, SubscriberPort
from airo_ipc.errors import ClosedError
from airo_ipc.message import FLAG_LATCHED, Message
from airo_ipc.transport import Transport

__all__ = ["Publisher", "Subscriber", "Client", "Server"]

logger = logging.getLogger("airo_ipc")

M = TypeVar("M", bound=Message)
Req = TypeVar("Req", bound=Message)
Rep = TypeVar("Rep", bound=Message)

#: Longest single blocking wait, so that Ctrl-C (KeyboardInterrupt) is handled quickly.
_WAIT_SLICE_SECONDS = 0.1


def _check_type(kind: str, expected: type[Message], msg: object) -> None:
    if type(msg) is not expected:
        raise TypeError(
            f"{kind} expects a {expected.__qualname__} message, got {type(msg).__qualname__}"
        )


class _Endpoint:
    def __init__(self, topic: str, transport: Transport) -> None:
        self._topic = topic
        self._transport = transport
        self._closed = False

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

    def close(self) -> None:
        raise NotImplementedError

    def __enter__(self: Any) -> Any:
        return self

    def __exit__(self, *exc: object) -> None:
        self.close()

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return f"<{type(self).__name__} '{self._topic}' {self._transport} {state}>"


class Publisher(_Endpoint, Generic[M]):
    """Sends messages of one type on one topic."""

    def __init__(
        self,
        port: PublisherPort,
        topic: str,
        msg_type: type[M],
        transport: Transport,
        latched: bool,
    ) -> None:
        super().__init__(topic, transport)
        self._port = port
        self._msg_type = msg_type
        self._flags = FLAG_LATCHED if latched else 0
        self._seq = itertools.count(1)
        self._lock = threading.Lock()

    @property
    def msg_type(self) -> type[M]:
        return self._msg_type

    @property
    def has_subscribers(self) -> bool:
        """True if at least one subscriber is connected."""
        self._check_open()
        return self._port.has_subscribers

    def publish(self, msg: M) -> None:
        """Send a copy of ``msg``. Writes the sequence number and time into ``msg.meta``."""
        self._check_open()
        _check_type("publish()", self._msg_type, msg)
        with self._lock:
            msg._airo_set_meta(next(self._seq), time.time_ns(), self._flags)
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
        self._check_open()
        msg, token = self._port.loan()
        msg._airo_begin_loan(readonly=False)
        try:
            yield msg  # type: ignore[misc]
        except BaseException:
            msg._airo_end_loan()
            self._port.discard(token)
            raise
        msg._airo_end_loan()
        with self._lock:
            msg._airo_set_meta(next(self._seq), time.time_ns(), self._flags)
            self._port.send(token)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._port.close()


class Subscriber(_Endpoint, Generic[M]):
    """Receives messages of one type on one topic."""

    def __init__(
        self, port: SubscriberPort, topic: str, msg_type: type[M], transport: Transport
    ) -> None:
        super().__init__(topic, transport)
        self._port = port
        self._msg_type = msg_type
        self._lock = threading.Lock()
        self._wait_lock = threading.Lock()  # backend wait objects are not shareable
        self._callback_thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._warned_escape = False

    @property
    def msg_type(self) -> type[M]:
        return self._msg_type

    @property
    def has_publishers(self) -> bool:
        """True if at least one publisher is known for this topic."""
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
        if self._port.owned_messages:
            return msg  # type: ignore[return-value]
        try:
            return msg.copy()  # type: ignore[return-value]
        finally:
            with self._lock:
                self._port.release(holder)

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
            if not escaped:
                with self._lock:
                    self._port.release(holder)
            elif not self._warned_escape:
                self._warned_escape = True
                logger.warning(
                    "A numpy view from receive_loan() on '%s' outlived its `with` block. The "
                    "shared memory stays borrowed until the view is garbage-collected; copy the "
                    "data inside the block instead.",
                    self._topic,
                )

    def _take(self, timeout: float | None) -> tuple[Message, Any] | None:
        self._check_open()
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {timeout}")
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            with self._lock:
                self._check_open()
                item = self._port.take()
            if item is not None:
                return item
            if deadline is None:
                wait = _WAIT_SLICE_SECONDS
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return None
                wait = min(remaining, _WAIT_SLICE_SECONDS)
            self._check_open()
            with self._wait_lock:
                self._port.wait(wait)

    def on_message(self, callback: Callable[[M], None]) -> None:
        """Call ``callback(msg)`` on a background thread for every received message.

        Exceptions in the callback are logged and do not stop the thread. Call ``close()`` to
        stop. Only one callback per subscriber.
        """
        self._check_open()
        if self._callback_thread is not None:
            raise RuntimeError(f"subscriber for '{self._topic}' already has a callback")

        def loop() -> None:
            while not self._stop.is_set():
                try:
                    msg = self.receive(timeout=_WAIT_SLICE_SECONDS)
                except ClosedError:
                    return
                if msg is None:
                    continue
                try:
                    callback(msg)
                except Exception:
                    logger.exception("on_message callback for '%s' raised", self._topic)

        self._callback_thread = threading.Thread(
            target=loop, name=f"airo_ipc-sub-{self._topic}", daemon=True
        )
        self._callback_thread.start()

    def close(self) -> None:
        if not self._closed:
            self._stop.set()
            self._closed = True  # waiting threads stop after their current wait slice
            thread = self._callback_thread
            if thread is not None and thread is not threading.current_thread():
                thread.join(timeout=2 * _WAIT_SLICE_SECONDS + 1.0)
            with self._wait_lock, self._lock:
                self._port.close()


class Client(_Endpoint, Generic[Req, Rep]):
    """Calls a service: sends a request and waits for the response."""

    def __init__(
        self,
        port: ClientPort,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
        thread_safe_port: bool,
    ) -> None:
        super().__init__(topic, transport)
        self._port = port
        self._request_type = request_type
        self._response_type = response_type
        self._lock: threading.Lock | None = (
            None if thread_safe_port else threading.Lock()
        )
        self._executor: concurrent.futures.ThreadPoolExecutor | None = None
        self._executor_lock = threading.Lock()

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
        deadline = None if timeout is None else time.monotonic() + timeout
        while not self.has_server:
            if deadline is not None and time.monotonic() >= deadline:
                return False
            time.sleep(0.01)
        return True

    def call(self, request: Req, timeout: float | None = 5.0) -> Rep:
        """Send ``request`` and return the response.

        Raises:
            NoServerError: no server is available (raised at once, without waiting).
            TimeoutError: the server did not reply within ``timeout`` seconds.
            ServiceError: the server's handler raised an exception.
        """
        self._check_open()
        _check_type("call()", self._request_type, request)
        if timeout is not None and timeout < 0:
            raise ValueError(f"timeout must be >= 0 or None, got {timeout}")
        if self._lock is None:
            response = self._port.call(request, timeout)
        else:
            with self._lock:
                response = self._port.call(request, timeout)
        _check_type("the server's response", self._response_type, response)
        return response  # type: ignore[return-value]

    def call_async(
        self, request: Req, timeout: float | None = 5.0
    ) -> concurrent.futures.Future[Rep]:
        """Like :meth:`call`, but return a :class:`concurrent.futures.Future` at once."""
        self._check_open()
        with self._executor_lock:
            if self._executor is None:
                self._executor = concurrent.futures.ThreadPoolExecutor(
                    max_workers=4, thread_name_prefix=f"airo_ipc-client-{self._topic}"
                )
        return self._executor.submit(self.call, request, timeout)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            if self._executor is not None:
                self._executor.shutdown(wait=True, cancel_futures=True)
            if self._lock is None:
                self._port.close()
            else:
                with self._lock:
                    self._port.close()


class Server(_Endpoint, Generic[Req, Rep]):
    """Serves a service: calls ``handler(request)`` and sends its return value back."""

    def __init__(
        self,
        port: ServerPort,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
        handler: Callable[[Req], Rep],
        spin: bool,
    ) -> None:
        super().__init__(topic, transport)
        self._port = port
        self._request_type = request_type
        self._response_type = response_type
        self._handler = handler
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        if spin:
            self._thread = threading.Thread(
                target=self._spin_loop, name=f"airo_ipc-server-{topic}", daemon=True
            )
            self._thread.start()

    @property
    def request_type(self) -> type[Req]:
        return self._request_type

    @property
    def response_type(self) -> type[Rep]:
        return self._response_type

    def spin_once(self, timeout: float = 0.0) -> bool:
        """Handle at most one request; wait up to ``timeout`` seconds for it.

        Use this with ``spin=False`` to handle requests in your own loop. Return True if a
        request was handled.
        """
        self._check_open()
        if self._thread is not None and threading.current_thread() is not self._thread:
            raise RuntimeError(
                "spin_once() is not allowed on a server created with spin=True"
            )
        deadline = time.monotonic() + max(0.0, timeout)
        while True:
            remaining = deadline - time.monotonic()
            with self._lock:
                if self._closed:
                    return False
                item = self._port.receive(max(0.0, min(remaining, _WAIT_SLICE_SECONDS)))
            if item is not None:
                break
            if time.monotonic() >= deadline or self._closed:
                return False
        request, token = item
        try:
            response = self._handler(request)  # type: ignore[arg-type]
            _check_type(
                f"the handler of '{self._topic}'", self._response_type, response
            )
        except Exception as e:
            logger.exception("handler of service '%s' raised", self._topic)
            with self._lock:
                if not self._closed:
                    self._port.reply_error(token, f"{type(e).__name__}: {e}")
            return True
        with self._lock:
            if not self._closed:
                self._port.reply(token, response)
        return True

    def _spin_loop(self) -> None:
        while not self._stop.is_set():
            try:
                self.spin_once(_WAIT_SLICE_SECONDS)
            except ClosedError:
                return
            except Exception:  # pragma: no cover - keep serving
                logger.exception("server loop of '%s' failed", self._topic)

    def close(self) -> None:
        if not self._closed:
            self._stop.set()
            self._closed = True
            if (
                self._thread is not None
                and self._thread is not threading.current_thread()
            ):
                self._thread.join(timeout=2 * _WAIT_SLICE_SECONDS + 1.0)
            with self._lock:
                self._port.close()
