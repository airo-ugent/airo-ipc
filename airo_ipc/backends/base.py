"""Interfaces between the public API and the libraries that move the bytes.

airo-ipc sends messages with two libraries: iceoryx2 (shared memory, between processes on one
host) and Zenoh (network, and services). A *backend* is the module that wraps one of them:
``iox2_backend.py`` and ``zenoh_backend.py``.

The public classes ``Publisher``, ``Subscriber``, ``Client`` and ``Server`` (in
:mod:`airo_ipc.endpoints`) do the work that is the same for both libraries: sequence numbers,
timeouts, type checks and message loans (a *loan* is a message that the user writes in place,
in memory that the backend provides). Each of them has one *driver* and calls it for
everything that depends on the underlying library.

A driver is an object with the methods of one of the four classes below, for example
``PublisherDriver.send_copy(msg)``. Drivers only move bytes and contain no other logic. Put new
behaviour in ``endpoints.py`` when both libraries need it.

Threads: a ``Publisher`` or ``Subscriber`` holds a lock during every call to its driver. The lock
protects the driver, which is not thread-safe, and lets ``close()`` run safely from another
thread. ``Client`` and ``Server`` use Zenoh drivers, which are thread-safe, so they take no lock.
"""

from __future__ import annotations

import time
from abc import ABC, abstractmethod
from collections.abc import Iterator
from typing import Any

from airo_ipc.message import Message

__all__ = [
    "PublisherDriver",
    "SubscriberDriver",
    "ClientDriver",
    "ServerDriver",
    "MAX_QUEUE_DEPTH",
    "check_timeout",
    "wait_slices",
]

#: Largest subscriber ``depth``. iceoryx2 fixes the buffer size when the first process creates a
#: topic, so every process uses this maximum and each subscriber picks its own depth below it.
MAX_QUEUE_DEPTH = 16

#: Longest single blocking wait, so that Ctrl-C (KeyboardInterrupt) and close() act quickly.
WAIT_SLICE_SECONDS = 0.1


def check_timeout(timeout: float | None) -> None:
    """Raise ValueError unless ``timeout`` is None or >= 0."""
    if timeout is not None and timeout < 0:
        raise ValueError(f"timeout must be >= 0 or None, got {timeout}")


def wait_slices(timeout: float | None) -> Iterator[float]:
    """Wait times for a loop that polls, waits the yielded time, and polls again.

    Yields slices of at most :data:`WAIT_SLICE_SECONDS` until ``timeout`` seconds pass (forever
    if ``timeout`` is None), then a final ``0.0`` so that the caller polls once after the deadline.

    Raises:
        ValueError: ``timeout`` is negative.
    """
    check_timeout(timeout)
    return _slices(None if timeout is None else time.monotonic() + timeout)


def _slices(deadline: float | None) -> Iterator[float]:
    while True:
        if deadline is None:
            yield WAIT_SLICE_SECONDS
            continue
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            yield 0.0
            return
        yield min(remaining, WAIT_SLICE_SECONDS)


class PublisherDriver(ABC):
    @abstractmethod
    def loan(self) -> tuple[Message, Any]:
        """Return a message placed in send memory, and a token for `send` / `discard`."""

    @abstractmethod
    def send(self, token: Any) -> None:
        """Send the loaned message."""

    @abstractmethod
    def discard(self, token: Any) -> None:
        """Give the loaned memory back without sending."""

    @abstractmethod
    def send_copy(self, msg: Message) -> None:
        """Send a copy of ``msg``."""

    @property
    @abstractmethod
    def has_subscribers(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...


class SubscriberDriver(ABC):
    @abstractmethod
    def take(self) -> tuple[Message, Any] | None:
        """Return the next message and a holder for `release`, or None.

        A holder of None means that the caller owns the message (no `release` needed).
        """

    @abstractmethod
    def release(self, holder: Any) -> None:
        """Release the memory of a message returned by `take`."""

    @abstractmethod
    def wait(self, timeout: float) -> None:
        """Block until new data may be available or ``timeout`` seconds pass."""

    @property
    @abstractmethod
    def has_publishers(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...


class ClientDriver(ABC):
    @abstractmethod
    def call(self, request: Message, timeout: float | None) -> Message:
        """Send ``request`` and return an owned response.

        Raises:
            NoServerError: no server is available.
            TimeoutError: no response within ``timeout`` seconds.
            ServiceError: the server handler raised an exception.
        """

    @property
    @abstractmethod
    def has_server(self) -> bool: ...

    @abstractmethod
    def close(self) -> None: ...


class ServerDriver(ABC):
    @abstractmethod
    def receive(self, timeout: float) -> tuple[Message, Any] | None:
        """Wait up to ``timeout`` seconds; return an owned request and a reply token, or None."""

    @abstractmethod
    def reply(self, token: Any, response: Message) -> None: ...

    @abstractmethod
    def reply_error(self, token: Any, error: BaseException) -> None:
        """Send ``error`` to the client, which raises it as ``ServiceError("<Type>: <text>")``."""

    @abstractmethod
    def close(self) -> None: ...
