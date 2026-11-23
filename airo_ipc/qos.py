"""Subscriber quality of service: which received messages ``receive()`` returns.

The same QoS works on both transports.
"""

from __future__ import annotations

from dataclasses import dataclass

__all__ = ["Latest", "Queue", "QoS", "MAX_QUEUE_DEPTH"]

#: Largest supported `Queue` depth. iceoryx2 fixes the buffer size when the first process creates
#: a topic, so every process uses this maximum and each subscriber picks its own depth below it.
MAX_QUEUE_DEPTH = 16


@dataclass(frozen=True)
class Latest:
    """``receive()`` returns the newest message. Older unread messages are dropped.

    The right choice for sensor streams such as camera frames, where only the newest value
    matters. This is the default.
    """

    @property
    def depth(self) -> int:
        return 1


@dataclass(frozen=True)
class Queue:
    """``receive()`` returns messages in order, up to ``depth`` unread messages.

    When the queue is full, the oldest unread message is dropped; the publisher never blocks.
    """

    depth: int

    def __post_init__(self) -> None:
        if isinstance(self.depth, bool) or not isinstance(self.depth, int):
            raise TypeError(f"Queue depth must be an int, got {self.depth!r}")
        if not 1 <= self.depth <= MAX_QUEUE_DEPTH:
            raise ValueError(
                f"Queue depth must be between 1 and {MAX_QUEUE_DEPTH}, got {self.depth}"
            )


QoS = Latest | Queue
