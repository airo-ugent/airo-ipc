"""iceoryx2 backend for publish/subscribe on ``Local()``.

iceoryx2 passes messages between processes on one host through shared memory, without copying
them. This module implements the publisher and subscriber drivers of :mod:`airo_ipc.backends.base`
with it. Services on ``Local()`` use Zenoh instead (see :class:`~airo_ipc.transport.Local`).

Design notes (verified with iceoryx2 0.10.1):

- Every topic has a second iceoryx2 service, ``<topic>/@evt``, that carries only wake-up
  signals. Publishers signal it after each send, so subscribers can block in ``wait()`` instead
  of polling.
- Topics keep no history: a subscriber receives only messages published after it connected.
- iceoryx2 fixes the size of a topic when the first process creates it; later processes may only
  ask for the same size or less. The sizes are therefore fixed constants of this module, the same
  in every process, and generous. Generous sizes are cheap: iceoryx2 reserves the worst-case
  shared memory, but the operating system only backs the chunks that are written. Measured for a
  4.6 MB message (1280x720 RGB-D) and 8 subscribers: 235 MB reserved, 23 MB used after 30 sends.
"""

from __future__ import annotations

import re
import time
from typing import Any

import iceoryx2 as iox2

from airo_ipc.backends.base import MAX_QUEUE_DEPTH, PublisherDriver, SubscriberDriver
from airo_ipc.errors import AiroIpcError, SchemaMismatchError, TooManyLoansError
from airo_ipc.message import Message

__all__ = ["Iox2Backend"]

iox2.set_log_level_from_env_or(iox2.LogLevel.Warn)

#: Publishers per topic. More than one allows a warm restart.
MAX_PUBLISHERS = 4
#: Subscribers per topic.
MAX_SUBSCRIBERS = 16
#: ``receive_loan()`` messages that one subscriber may hold at the same time.
MAX_LOANS_PER_SUBSCRIBER = 4

_EVENT_SUFFIX = "/@evt"
_CLEANUP_INTERVAL_SECONDS = 0.5


def _notify(notifier: Any) -> None:
    try:
        notifier.notify()
    except Exception:  # pragma: no cover - a full listener queue only delays a wake-up
        pass


def _wait(listener: Any, seconds: float) -> None:
    """Wait for events, at most ``seconds``. Consumes the pending events.

    iceoryx2 0.10.1 treats ``timed_wait(Duration 0)`` as "block forever", so a zero timeout
    uses the non-blocking ``try_wait()``.
    """
    if seconds <= 0:
        listener.try_wait()
    else:
        listener.timed_wait(iox2.Duration.from_secs_f64(seconds))


def _open_error(what: str, topic: str, error: Exception) -> AiroIpcError:
    text = f"{type(error).__name__}: {error}"
    if re.search(r"Incompatible\w*Type", text):
        return SchemaMismatchError(
            f"cannot open {what} '{topic}' on Local(): the topic already exists with a different "
            f"message type (class name, fields, dtypes or shapes differ). Use the same message "
            f"definition in every process. ({text})"
        )
    if "DoesNotSupport" in text or "Incompatible" in text:
        return AiroIpcError(
            f"cannot open {what} '{topic}' on Local(): another process created it with "
            f"different settings, probably with another airo-ipc version. Use the same airo-ipc "
            f"version in every process. ({text})"
        )
    if "ExceedsMax" in text:
        return AiroIpcError(
            f"cannot open {what} '{topic}' on Local(): too many endpoints for this topic (at most "
            f"{MAX_PUBLISHERS} publishers and {MAX_SUBSCRIBERS} subscribers). Close unused "
            f"endpoints. ({text})"
        )
    return AiroIpcError(f"cannot open {what} '{topic}' on Local(): {text}")


class _Iox2Driver:
    """Base of every iceoryx2 driver: it owns ``self._service``."""

    _service: Any
    _last_cleanup = 0.0

    def _dynamic_config(self) -> Any:
        """The service's live endpoint counts, after removing endpoints of killed processes.

        iceoryx2 keeps counting the endpoints of a process that died without cleanup (SIGKILL)
        until a dead-node cleanup runs. The cleanup scans the node list, so it is rate-limited.
        """
        now = time.monotonic()
        if now - self._last_cleanup >= _CLEANUP_INTERVAL_SECONDS:
            self._last_cleanup = now
            try:
                self._service.try_cleanup_dead_nodes()
            except Exception:  # pragma: no cover - best effort
                pass
        return self._service.dynamic_config


class Iox2Backend:
    """Owns the iceoryx2 node of one :class:`~airo_ipc.context.Context`."""

    def __init__(self) -> None:
        self._node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
        try:
            # Remove shared memory left behind by processes that were killed.
            self._node.try_cleanup_dead_nodes()
        except Exception:  # pragma: no cover - best effort
            pass

    def pubsub_service(self, topic: str, msg_type: type[Message]) -> Any:
        try:
            return (
                self._node.service_builder(iox2.ServiceName.new(topic))
                .publish_subscribe(msg_type)
                .max_publishers(MAX_PUBLISHERS)
                .max_subscribers(MAX_SUBSCRIBERS)
                .history_size(0)
                .subscriber_max_buffer_size(MAX_QUEUE_DEPTH)
                .subscriber_max_borrowed_samples(MAX_LOANS_PER_SUBSCRIBER)
                .enable_safe_overflow(True)
                .open_or_create()
            )
        except Exception as e:
            raise _open_error("topic", topic, e) from e

    def topic_events(self, topic: str) -> Any:
        """The event service that wakes the subscribers of ``topic``."""
        name = topic + _EVENT_SUFFIX
        try:
            return (
                self._node.service_builder(iox2.ServiceName.new(name))
                .event()
                .max_listeners(MAX_SUBSCRIBERS)
                .max_notifiers(MAX_PUBLISHERS)
                .open_or_create()
            )
        except Exception as e:
            raise _open_error("event service", name, e) from e

    def publisher(self, topic: str, msg_type: type[Message]) -> PublisherDriver:
        return Iox2PublisherDriver(self, topic, msg_type)

    def subscriber(
        self, topic: str, msg_type: type[Message], depth: int
    ) -> SubscriberDriver:
        return Iox2SubscriberDriver(self, topic, msg_type, depth)

    def close(self) -> None:
        self._node = None


# ------------------------------------------------------------------------------------------------
# Publish / subscribe
# ------------------------------------------------------------------------------------------------


class Iox2PublisherDriver(_Iox2Driver, PublisherDriver):
    def __init__(
        self, backend: Iox2Backend, topic: str, msg_type: type[Message]
    ) -> None:
        self._service = backend.pubsub_service(topic, msg_type)
        try:
            self._publisher = self._service.publisher_builder().create()
        except Exception as e:
            raise _open_error("publisher", topic, e) from e
        self._events = backend.topic_events(topic)
        self._notifier = self._events.notifier_builder().create()

    def loan(self) -> tuple[Message, Any]:
        sample = self._publisher.loan_uninit()
        msg: Message = sample.payload().contents
        return msg, sample

    def send(self, token: Any) -> None:
        token.assume_init().send()
        _notify(self._notifier)

    def discard(self, token: Any) -> None:
        token.delete()

    def send_copy(self, msg: Message) -> None:
        self._publisher.send_copy(msg)
        _notify(self._notifier)

    @property
    def has_subscribers(self) -> bool:
        return bool(self._dynamic_config().number_of_subscribers > 0)

    def close(self) -> None:
        self._publisher.delete()
        self._notifier.delete()
        self._service = self._events = None  # frees the service records


class Iox2SubscriberDriver(_Iox2Driver, SubscriberDriver):
    def __init__(
        self, backend: Iox2Backend, topic: str, msg_type: type[Message], depth: int
    ) -> None:
        self._topic = topic
        self._service = backend.pubsub_service(topic, msg_type)
        try:
            self._subscriber = (
                self._service.subscriber_builder().buffer_size(depth).create()
            )
        except Exception as e:
            raise _open_error("subscriber", topic, e) from e
        self._events = backend.topic_events(topic)
        self._listener = self._events.listener_builder().create()

    def take(self) -> tuple[Message, Any] | None:
        try:
            sample = self._subscriber.receive()
        except Exception as e:
            if "ExceedsMaxBorrows" in str(e):
                raise TooManyLoansError(
                    f"subscriber on '{self._topic}' already holds "
                    f"{MAX_LOANS_PER_SUBSCRIBER} received loans. Let receive_loan() views go "
                    f"out of scope, or copy the data inside the `with` block."
                ) from e
            raise
        if sample is None:
            return None
        msg: Message = sample.payload().contents
        return msg, sample

    def release(self, holder: Any) -> None:
        holder.delete()

    def wait(self, timeout: float) -> None:
        _wait(self._listener, timeout)

    @property
    def has_publishers(self) -> bool:
        return bool(self._dynamic_config().number_of_publishers > 0)

    def close(self) -> None:
        self._subscriber.delete()
        self._listener.delete()
        self._service = self._events = None  # frees the service records
