"""iceoryx2 backend for ``Local()``: zero-copy shared memory on one host.

Design notes (verified with iceoryx2 0.10.1):

- Every pub/sub topic has a companion event service ``<topic>/@evt``. Publishers notify it after
  each send, so subscribers can block in ``wait()`` instead of polling.
- Services are sized with :class:`~airo_ipc.defaults.Iox2Sizing`. iceoryx2 fixes the sizes when
  the first process creates a service, so all processes must use the same sizing.
- Every topic keeps a history of 1 message. A subscriber drops history messages from publishers
  that are not latched (see ``FLAG_LATCHED``), so only latched publishers reach late joiners.
- iceoryx2 delivers history only when a publisher updates its connections. A background thread
  does that for latched publishers.
"""

from __future__ import annotations

import ctypes
import re
import threading
import time
import weakref
from typing import Any

import iceoryx2 as iox2

from airo_ipc.backends.base import ClientPort, PublisherPort, ServerPort, SubscriberPort
from airo_ipc.defaults import Iox2Sizing
from airo_ipc.errors import (
    AiroIpcError,
    NoServerError,
    SchemaMismatchError,
    ServiceError,
    TooManyLoansError,
)
from airo_ipc.message import FLAG_LATCHED, Message
from airo_ipc.qos import MAX_QUEUE_DEPTH, QoS

__all__ = ["Iox2Backend"]

iox2.set_log_level_from_env_or(iox2.LogLevel.Warn)

_EVENT_SUFFIX = "/@evt"
_REQUEST_EVENT_SUFFIX = "/@req"
_RESPONSE_EVENT_SUFFIX = "/@rep"
_LATCH_REFRESH_SECONDS = 0.05


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
    if "IncompatibleMessagingPattern" in text:
        return AiroIpcError(
            f"cannot open {what} '{topic}' on Local(): the name is already used by a different "
            f"kind of endpoint (topic vs. service). ({text})"
        )
    if "DoesNotSupport" in text or "Incompatible" in text:
        return AiroIpcError(
            f"cannot open {what} '{topic}' on Local(): another process created it with a "
            f"different Iox2Sizing. Use the same sizing in every process. ({text})"
        )
    if "ExceedsMax" in text:
        return AiroIpcError(
            f"cannot open {what} '{topic}' on Local(): too many endpoints for this topic. Raise "
            f"the limit with Context(iox2=Iox2Sizing(...)) in every process. ({text})"
        )
    return AiroIpcError(f"cannot open {what} '{topic}' on Local(): {text}")


_CLEANUP_INTERVAL_SECONDS = 0.5
_last_cleanup: dict[int, float] = {}


def _dynamic_config(service: Any) -> Any:
    """The service's live endpoint counts, after removing endpoints of killed processes.

    iceoryx2 keeps counting the endpoints of a process that died without cleanup (SIGKILL)
    until a dead-node cleanup runs. The cleanup scans the node list, so it is rate-limited.
    """
    now = time.monotonic()
    key = id(service)
    if now - _last_cleanup.get(key, 0.0) >= _CLEANUP_INTERVAL_SECONDS:
        _last_cleanup[key] = now
        try:
            service.try_cleanup_dead_nodes()
        except Exception:  # pragma: no cover - best effort
            pass
    return service.dynamic_config


class Iox2Backend:
    """Owns the iceoryx2 node of one :class:`~airo_ipc.context.Context`."""

    def __init__(self, sizing: Iox2Sizing) -> None:
        self.sizing = sizing
        self._node = iox2.NodeBuilder.new().create(iox2.ServiceType.Ipc)
        try:
            # Remove shared memory left behind by processes that were killed.
            self._node.try_cleanup_dead_nodes()
        except Exception:  # pragma: no cover - best effort
            pass
        self._latched: weakref.WeakSet[Iox2PublisherPort] = weakref.WeakSet()
        self._latch_thread: threading.Thread | None = None
        self._stop = threading.Event()

    # -- services -------------------------------------------------------------------------

    def pubsub_service(self, topic: str, msg_type: type[Message]) -> Any:
        s = self.sizing
        try:
            return (
                self._node.service_builder(iox2.ServiceName.new(topic))
                .publish_subscribe(msg_type)
                .max_publishers(s.max_publishers)
                .max_subscribers(s.max_subscribers)
                .history_size(1)
                .subscriber_max_buffer_size(MAX_QUEUE_DEPTH)
                .subscriber_max_borrowed_samples(s.max_borrowed_per_subscriber)
                .enable_safe_overflow(True)
                .open_or_create()
            )
        except Exception as e:
            raise _open_error("topic", topic, e) from e

    def event_service(self, name: str, *, listeners: int, notifiers: int) -> Any:
        try:
            return (
                self._node.service_builder(iox2.ServiceName.new(name))
                .event()
                .max_listeners(listeners)
                .max_notifiers(notifiers)
                .open_or_create()
            )
        except Exception as e:
            raise _open_error("event service", name, e) from e

    def reqrep_service(
        self,
        topic: str,
        request_type: type[Message],
        envelope_type: type[ctypes.Structure],
    ) -> Any:
        s = self.sizing
        try:
            return (
                self._node.service_builder(iox2.ServiceName.new(topic))
                .request_response(request_type, envelope_type)
                .max_clients(s.max_clients)
                .max_servers(s.max_servers)
                .max_active_requests_per_client(s.max_active_requests_per_client)
                .enable_safe_overflow_for_requests(False)
                .enable_safe_overflow_for_responses(False)
                .open_or_create()
            )
        except Exception as e:
            raise _open_error("service", topic, e) from e

    # -- ports ----------------------------------------------------------------------------

    def publisher(
        self, topic: str, msg_type: type[Message], latched: bool
    ) -> PublisherPort:
        port = Iox2PublisherPort(self, topic, msg_type)
        if latched:
            self._latched.add(port)
            self._ensure_latch_thread()
        return port

    def subscriber(
        self, topic: str, msg_type: type[Message], qos: QoS
    ) -> SubscriberPort:
        return Iox2SubscriberPort(self, topic, msg_type, qos)

    def client(
        self, topic: str, request_type: type[Message], response_type: type[Message]
    ) -> ClientPort:
        return Iox2ClientPort(self, topic, request_type, response_type)

    def server(
        self, topic: str, request_type: type[Message], response_type: type[Message]
    ) -> ServerPort:
        return Iox2ServerPort(self, topic, request_type, response_type)

    def close(self) -> None:
        self._stop.set()
        if self._latch_thread is not None:
            self._latch_thread.join(timeout=1.0)
        self._node = None

    # -- latched publishers ---------------------------------------------------------------

    def _ensure_latch_thread(self) -> None:
        if self._latch_thread is None:
            self._latch_thread = threading.Thread(
                target=self._latch_loop, name="airo_ipc-iox2-latch", daemon=True
            )
            self._latch_thread.start()

    def _latch_loop(self) -> None:
        while not self._stop.wait(_LATCH_REFRESH_SECONDS):
            for port in list(self._latched):
                port.update_connections()


# ------------------------------------------------------------------------------------------------
# Publish / subscribe
# ------------------------------------------------------------------------------------------------


class Iox2PublisherPort(PublisherPort):
    def __init__(
        self, backend: Iox2Backend, topic: str, msg_type: type[Message]
    ) -> None:
        self._topic = topic
        self._service = backend.pubsub_service(topic, msg_type)
        try:
            self._publisher = self._service.publisher_builder().create()
        except Exception as e:
            raise _open_error("publisher", topic, e) from e
        s = backend.sizing
        events = backend.event_service(
            topic + _EVENT_SUFFIX,
            listeners=s.max_subscribers,
            notifiers=s.max_publishers,
        )
        self._notifier = events.notifier_builder().create()
        self._events = events
        self._lock = threading.Lock()  # the latch thread also uses the publisher
        self._closed = False

    def loan(self) -> tuple[Message, Any]:
        with self._lock:
            sample = self._publisher.loan_uninit()
        msg: Message = sample.payload().contents
        return msg, sample

    def send(self, token: Any) -> None:
        with self._lock:
            token.assume_init().send()
            self._notify()

    def discard(self, token: Any) -> None:
        with self._lock:
            token.delete()

    def send_copy(self, msg: Message) -> None:
        with self._lock:
            self._publisher.send_copy(msg)
            self._notify()

    def _notify(self) -> None:
        try:
            self._notifier.notify()
        except (
            Exception
        ):  # pragma: no cover - a full listener queue only delays a wake-up
            pass

    def update_connections(self) -> None:
        with self._lock:
            if not self._closed:
                try:
                    self._publisher.update_connections()
                except Exception:  # pragma: no cover - best effort
                    pass

    @property
    def has_subscribers(self) -> bool:
        return bool(_dynamic_config(self._service).number_of_subscribers > 0)

    def close(self) -> None:
        with self._lock:
            if not self._closed:
                self._closed = True
                self._publisher.delete()
                self._notifier.delete()
                self._service = self._events = None  # frees the service records


class Iox2SubscriberPort(SubscriberPort):
    owned_messages = False

    def __init__(
        self, backend: Iox2Backend, topic: str, msg_type: type[Message], qos: QoS
    ) -> None:
        self._topic = topic
        self._service = backend.pubsub_service(topic, msg_type)
        try:
            self._subscriber = (
                self._service.subscriber_builder().buffer_size(qos.depth).create()
            )
        except Exception as e:
            raise _open_error("subscriber", topic, e) from e
        s = backend.sizing
        events = backend.event_service(
            topic + _EVENT_SUFFIX,
            listeners=s.max_subscribers,
            notifiers=s.max_publishers,
        )
        self._listener = events.listener_builder().create()
        self._events = events
        self._max_borrowed = s.max_borrowed_per_subscriber
        self._created_ns = time.time_ns()
        self._closed = False

    def take(self) -> tuple[Message, Any] | None:
        while True:
            try:
                sample = self._subscriber.receive()
            except Exception as e:
                if "ExceedsMaxBorrows" in str(e):
                    raise TooManyLoansError(
                        f"subscriber on '{self._topic}' already holds "
                        f"{self._max_borrowed} received loans. Let receive_loan() views go out "
                        f"of scope (or copy them), or raise "
                        f"Iox2Sizing.max_borrowed_per_subscriber."
                    ) from e
                raise
            if sample is None:
                return None
            msg: Message = sample.payload().contents
            if (
                not (msg._airo_flags & FLAG_LATCHED)
                and msg._airo_stamp_ns < self._created_ns
            ):
                sample.delete()  # history from a non-latched publisher: not for late joiners
                continue
            return msg, sample

    def release(self, holder: Any) -> None:
        holder.delete()

    def wait(self, timeout: float) -> None:
        _wait(self._listener, timeout)

    @property
    def has_publishers(self) -> bool:
        return bool(_dynamic_config(self._service).number_of_publishers > 0)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._subscriber.delete()
            self._listener.delete()
            self._service = self._events = None  # frees the service records


# ------------------------------------------------------------------------------------------------
# Services
# ------------------------------------------------------------------------------------------------

_ERROR_TEXT_SIZE = 512
_STATUS_OK = 0
_STATUS_ERROR = 1
_envelopes: dict[type[Message], type[ctypes.Structure]] = {}


def _envelope_type(response_type: type[Message]) -> type[ctypes.Structure]:
    """A response wrapper that can carry either the response or an error text."""
    cached = _envelopes.get(response_type)
    if cached is not None:
        return cached
    type_name = f"airo_ipc::Reply<{response_type.type_name()}>"

    class _Envelope(ctypes.Structure):
        _fields_ = [
            ("status", ctypes.c_uint32),
            ("error_len", ctypes.c_uint32),
            ("error", ctypes.c_char * _ERROR_TEXT_SIZE),
            ("payload", response_type),
        ]

        @staticmethod
        def type_name() -> str:
            return type_name

    _Envelope.__name__ = f"Reply_{response_type.__name__}"
    _envelopes[response_type] = _Envelope
    return _Envelope


class Iox2ClientPort(ClientPort):
    def __init__(
        self,
        backend: Iox2Backend,
        topic: str,
        request_type: type[Message],
        response_type: type[Message],
    ) -> None:
        self._topic = topic
        self._response_type = response_type
        self._service = backend.reqrep_service(
            topic, request_type, _envelope_type(response_type)
        )
        try:
            self._client = self._service.client_builder().create()
        except Exception as e:
            raise _open_error("client", topic, e) from e
        s = backend.sizing
        req_events = backend.event_service(
            topic + _REQUEST_EVENT_SUFFIX,
            listeners=s.max_servers,
            notifiers=s.max_clients,
        )
        rep_events = backend.event_service(
            topic + _RESPONSE_EVENT_SUFFIX,
            listeners=s.max_clients,
            notifiers=s.max_servers,
        )
        self._notifier = req_events.notifier_builder().create()
        self._listener = rep_events.listener_builder().create()
        self._events: Any = (req_events, rep_events)
        self._closed = False

    def call(self, request: Message, timeout: float | None) -> Message:
        if not self.has_server:
            raise NoServerError(f"no server for service '{self._topic}' on Local()")
        pending = self._client.send_copy(request)
        try:
            self._notifier.notify()
        except Exception:  # pragma: no cover
            pass
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            connected = (
                pending.is_connected
            )  # read before receive(): no lost last response
            response = pending.receive()
            if response is not None:
                envelope = response.payload().contents
                try:
                    if envelope.status != _STATUS_OK:
                        text = bytes(envelope.error[: envelope.error_len]).decode(
                            "utf-8", errors="replace"
                        )
                        raise ServiceError(text)
                    result: Message = envelope.payload.copy()
                    result.check_schema()
                    return result
                finally:
                    response.delete()
            if not connected:
                raise NoServerError(
                    f"the server of service '{self._topic}' disconnected before it replied"
                )
            if deadline is None:
                wait = 0.1
            else:
                wait = min(0.1, deadline - time.monotonic())
                if wait <= 0:
                    raise TimeoutError(
                        f"no response from service '{self._topic}' within {timeout} s"
                    )
            _wait(self._listener, wait)

    @property
    def has_server(self) -> bool:
        return bool(_dynamic_config(self._service).number_of_servers > 0)

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._client.delete()
            self._notifier.delete()
            self._listener.delete()
            self._service = self._events = None  # frees the service records


class Iox2ServerPort(ServerPort):
    def __init__(
        self,
        backend: Iox2Backend,
        topic: str,
        request_type: type[Message],
        response_type: type[Message],
    ) -> None:
        self._topic = topic
        self._service = backend.reqrep_service(
            topic, request_type, _envelope_type(response_type)
        )
        try:
            self._server = self._service.server_builder().create()
        except Exception as e:
            raise _open_error("server", topic, e) from e
        s = backend.sizing
        req_events = backend.event_service(
            topic + _REQUEST_EVENT_SUFFIX,
            listeners=s.max_servers,
            notifiers=s.max_clients,
        )
        rep_events = backend.event_service(
            topic + _RESPONSE_EVENT_SUFFIX,
            listeners=s.max_clients,
            notifiers=s.max_servers,
        )
        self._listener = req_events.listener_builder().create()
        self._notifier = rep_events.notifier_builder().create()
        self._events: Any = (req_events, rep_events)
        self._closed = False

    def receive(self, timeout: float) -> tuple[Message, Any] | None:
        active = self._server.receive()
        if active is None:
            _wait(self._listener, timeout)
            active = self._server.receive()
            if active is None:
                return None
        view: Message = active.payload().contents
        request = view.copy()
        request.check_schema()
        return request, active

    def reply(self, token: Any, response: Message) -> None:
        self._send(token, _STATUS_OK, b"", response)

    def reply_error(self, token: Any, error: str) -> None:
        self._send(token, _STATUS_ERROR, error.encode("utf-8"), None)

    def _send(
        self, active: Any, status: int, error: bytes, response: Message | None
    ) -> None:
        try:
            uninit = active.loan_uninit()
            envelope = uninit.payload().contents
            envelope.status = status
            error = error[:_ERROR_TEXT_SIZE]
            envelope.error_len = len(error)
            envelope.error = error
            if response is not None:
                ctypes.memmove(
                    ctypes.addressof(envelope.payload),
                    ctypes.addressof(response),
                    ctypes.sizeof(response),
                )
            uninit.assume_init().send()
        finally:
            active.delete()  # tells the client that no more responses follow
        try:
            self._notifier.notify()
        except Exception:  # pragma: no cover
            pass

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._server.delete()
            self._listener.delete()
            self._notifier.delete()
            self._service = self._events = None  # frees the service records
