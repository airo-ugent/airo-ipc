"""The small interface that every transport backend implements.

The public endpoint classes in :mod:`airo_ipc.endpoints` hold all shared logic (sequence numbers,
loans, timeouts, type checks, threads). A backend only moves bytes. Backends are not thread-safe;
the endpoints serialize calls with a lock.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from typing import Any

from airo_ipc.message import Message

__all__ = ["PublisherPort", "SubscriberPort", "ClientPort", "ServerPort"]


class PublisherPort(ABC):
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


class SubscriberPort(ABC):
    #: True if `take` returns messages that the caller owns (no `release` needed).
    owned_messages: bool = False

    @abstractmethod
    def take(self) -> tuple[Message, Any] | None:
        """Return the next message (per the QoS) and a holder for `release`, or None."""

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


class ClientPort(ABC):
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


class ServerPort(ABC):
    @abstractmethod
    def receive(self, timeout: float) -> tuple[Message, Any] | None:
        """Wait up to ``timeout`` seconds; return an owned request and a reply token, or None."""

    @abstractmethod
    def reply(self, token: Any, response: Message) -> None: ...

    @abstractmethod
    def reply_error(self, token: Any, error: str) -> None: ...

    @abstractmethod
    def close(self) -> None: ...
