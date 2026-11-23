"""Zenoh backend for ``Network(...)``: between hosts, and between processes on one host.

Design notes (verified with eclipse-zenoh 1.10.2):

- Wire format: the raw message bytes (header + fields), see :meth:`Message.to_bytes`.
- Key expressions: ``airo_ipc/<namespace>/<topic>`` for topics, ``.../<topic>/@srv`` for
  services, and liveliness tokens under ``.../<topic>/@pub/<id>`` and ``.../<topic>/@sub/<id>``
  for ``has_publishers`` / ``has_subscribers``.
- Zenoh's ``Handler.recv()`` has no timeout, so subscribers use a Python callback and a
  ``threading.Condition``.
- A Python callback keeps the interpreter from exiting until its session is closed, and
  ``atexit`` runs too late for that. Sessions are therefore closed from a
  ``threading._register_atexit`` hook, which runs before Python joins non-daemon threads.
- Latched publishers use the zenoh-ext advanced publisher cache; this needs
  ``timestamping/enabled``. Subscribers are always advanced subscribers that ask late
  publishers for their history.
- Zenoh's own shared-memory transport is disabled unless a config file is given.
"""

from __future__ import annotations

import collections
import json
import logging
import queue
import threading
import time
import uuid
import weakref
from typing import Any

import zenoh
import zenoh.ext as zext

from airo_ipc.backends.base import ClientPort, PublisherPort, ServerPort, SubscriberPort
from airo_ipc.errors import (
    AiroIpcError,
    NoServerError,
    SchemaMismatchError,
    ServiceError,
)
from airo_ipc.message import Message
from airo_ipc.qos import QoS
from airo_ipc.transport import Network, ResolvedNetwork

__all__ = ["ZenohBackend"]

logger = logging.getLogger("airo_ipc")

zenoh.init_log_from_env_or("error")

#: Used for `timeout=None` (Zenoh needs a finite query timeout).
_FOREVER_SECONDS = 365 * 24 * 3600.0

_backends: weakref.WeakSet[ZenohBackend] = weakref.WeakSet()
_hook_lock = threading.Lock()
_hook_registered = False


def _close_all_backends() -> None:
    for backend in list(_backends):
        try:
            backend.close()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass


def _register_exit_hook() -> None:
    """Close sessions before Python waits for non-daemon threads at exit (see module doc)."""
    global _hook_registered
    with _hook_lock:
        if _hook_registered:
            return
        _hook_registered = True
        register = getattr(threading, "_register_atexit", None)
        if register is not None:
            try:
                register(_close_all_backends)
                return
            except RuntimeError:  # pragma: no cover - already shutting down
                return
        import atexit  # pragma: no cover - fallback for Pythons without the private hook

        atexit.register(_close_all_backends)  # pragma: no cover


def _make_config(net: ResolvedNetwork) -> zenoh.Config:
    config = zenoh.Config.from_file(net.config) if net.config else zenoh.Config()
    if net.mode is not None:
        config.insert_json5("mode", json.dumps(net.mode))
    if net.connect:
        config.insert_json5("connect/endpoints", json.dumps(list(net.connect)))
    if net.listen:
        config.insert_json5("listen/endpoints", json.dumps(list(net.listen)))
    if net.multicast is not None:
        config.insert_json5("scouting/multicast/enabled", json.dumps(net.multicast))
    config.insert_json5("timestamping/enabled", "true")
    if not net.config:
        config.insert_json5("transport/shared_memory/enabled", "false")
    return config


class ZenohBackend:
    """Owns the Zenoh sessions of one :class:`~airo_ipc.context.Context`.

    Endpoints whose ``Network(...)`` settings are equal (apart from the namespace) share a
    session.
    """

    #: Whether one client port may be used by several threads at once.
    clients_thread_safe = True

    def __init__(self) -> None:
        self._sessions: dict[tuple[object, ...], zenoh.Session] = {}
        self._lock = threading.Lock()
        self._closed = False
        _backends.add(self)
        _register_exit_hook()

    def session(self, net: ResolvedNetwork) -> zenoh.Session:
        key = net.session_key()
        with self._lock:
            if self._closed:
                raise AiroIpcError("the Zenoh backend is closed")
            session = self._sessions.get(key)
            if session is None:
                try:
                    session = zenoh.open(_make_config(net))
                except Exception as e:
                    raise AiroIpcError(
                        f"cannot open a Zenoh session for {net}: {type(e).__name__}: {e}"
                    ) from e
                self._sessions[key] = session
            return session

    # The factories take the same arguments as those of the iceoryx2 backend.

    def publisher(
        self, transport: Network, topic: str, msg_type: type[Message], latched: bool
    ) -> PublisherPort:
        net = transport.resolve()
        return ZenohPublisherPort(self.session(net), net, topic, msg_type, latched)

    def subscriber(
        self, transport: Network, topic: str, msg_type: type[Message], qos: QoS
    ) -> SubscriberPort:
        net = transport.resolve()
        return ZenohSubscriberPort(self.session(net), net, topic, msg_type, qos)

    def client(
        self, transport: Network, topic: str, req: type[Message], rep: type[Message]
    ) -> ClientPort:
        net = transport.resolve()
        return ZenohClientPort(self.session(net), net, topic, req, rep)

    def server(
        self, transport: Network, topic: str, req: type[Message], rep: type[Message]
    ) -> ServerPort:
        net = transport.resolve()
        return ZenohServerPort(self.session(net), net, topic, req, rep)

    def close(self) -> None:
        with self._lock:
            if self._closed:
                return
            self._closed = True
            sessions = list(self._sessions.values())
            self._sessions.clear()
        for session in sessions:
            try:
                session.close()
            except Exception:  # pragma: no cover - best effort
                pass


def _undeclare(*objects: Any) -> None:
    for obj in objects:
        if obj is None:
            continue
        try:
            obj.undeclare()
        except Exception:  # pragma: no cover - session already closed
            pass


class _Liveliness:
    """Tracks the liveliness tokens under one key expression (``.../@pub/*`` or ``.../@sub/*``)."""

    def __init__(self, session: zenoh.Session, key_expr: str) -> None:
        self._alive: set[str] = set()
        self._lock = threading.Lock()
        self._subscriber = session.liveliness().declare_subscriber(
            key_expr, self._on_sample, history=True
        )

    def _on_sample(self, sample: zenoh.Sample) -> None:
        key = str(sample.key_expr)
        with self._lock:
            if sample.kind == zenoh.SampleKind.PUT:
                self._alive.add(key)
            else:
                self._alive.discard(key)

    @property
    def any_alive(self) -> bool:
        with self._lock:
            return bool(self._alive)

    def close(self) -> None:
        _undeclare(self._subscriber)


# ------------------------------------------------------------------------------------------------
# Publish / subscribe
# ------------------------------------------------------------------------------------------------


class ZenohPublisherPort(PublisherPort):
    def __init__(
        self,
        session: zenoh.Session,
        net: ResolvedNetwork,
        topic: str,
        msg_type: type[Message],
        latched: bool,
    ) -> None:
        key = f"{net.key_prefix()}/{topic}"
        self._msg_type = msg_type
        self._publisher: Any
        if latched:
            self._publisher = zext.declare_advanced_publisher(
                session, key, cache=zext.CacheConfig(1), publisher_detection=True
            )
        else:
            self._publisher = session.declare_publisher(key)
        self._token = session.liveliness().declare_token(
            f"{key}/@pub/{uuid.uuid4().hex}"
        )
        self._subscribers = _Liveliness(session, f"{key}/@sub/*")
        self._buffer = bytearray(_sizeof(msg_type))
        self._buffer_in_use = False
        self._closed = False

    def loan(self) -> tuple[Message, Any]:
        if self._buffer_in_use:  # nested loans: give the inner one its own buffer
            buffer = bytearray(_sizeof(self._msg_type))
        else:
            self._buffer_in_use = True
            buffer = self._buffer
        return self._msg_type.from_buffer(buffer), buffer

    def send(self, token: Any) -> None:
        try:
            self._publisher.put(token)
        finally:
            self._release_buffer(token)

    def discard(self, token: Any) -> None:
        self._release_buffer(token)

    def _release_buffer(self, buffer: bytearray) -> None:
        if buffer is self._buffer:
            self._buffer_in_use = False

    def send_copy(self, msg: Message) -> None:
        self._publisher.put(msg.to_bytes())

    @property
    def has_subscribers(self) -> bool:
        return self._subscribers.any_alive

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._subscribers.close()
            _undeclare(self._token, self._publisher)


def _sizeof(msg_type: type[Message]) -> int:
    import ctypes

    return ctypes.sizeof(msg_type)


class ZenohSubscriberPort(SubscriberPort):
    owned_messages = True

    def __init__(
        self,
        session: zenoh.Session,
        net: ResolvedNetwork,
        topic: str,
        msg_type: type[Message],
        qos: QoS,
    ) -> None:
        key = f"{net.key_prefix()}/{topic}"
        self._topic = topic
        self._msg_type = msg_type
        self._queue: collections.deque[Message] = collections.deque(maxlen=qos.depth)
        self._error: Exception | None = None
        self._cond = threading.Condition()
        self._subscriber = zext.declare_advanced_subscriber(
            session,
            key,
            self._on_sample,
            history=zext.HistoryConfig(detect_late_publishers=True, max_samples=1),
        )
        self._token = session.liveliness().declare_token(
            f"{key}/@sub/{uuid.uuid4().hex}"
        )
        self._publishers = _Liveliness(session, f"{key}/@pub/*")
        self._closed = False

    def _on_sample(self, sample: zenoh.Sample) -> None:
        if sample.kind != zenoh.SampleKind.PUT:
            return
        try:
            msg = self._msg_type.from_bytes(sample.payload.to_bytes())
        except (SchemaMismatchError, ValueError) as e:
            with self._cond:
                self._error = e
                self._cond.notify_all()
            return
        with self._cond:
            self._queue.append(msg)
            self._cond.notify_all()

    def take(self) -> tuple[Message, Any] | None:
        with self._cond:
            if self._error is not None:
                error, self._error = self._error, None
                if isinstance(error, ValueError):
                    raise SchemaMismatchError(
                        f"received a message on '{self._topic}' with the wrong size: {error}"
                    ) from error
                raise error
            if self._queue:
                return self._queue.popleft(), None
            return None

    def release(self, holder: Any) -> None:
        pass

    def wait(self, timeout: float) -> None:
        with self._cond:
            if not self._queue and self._error is None:
                self._cond.wait(timeout)

    @property
    def has_publishers(self) -> bool:
        return self._publishers.any_alive

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._publishers.close()
            _undeclare(self._token, self._subscriber)
            with self._cond:
                self._cond.notify_all()


# ------------------------------------------------------------------------------------------------
# Services
# ------------------------------------------------------------------------------------------------


class ZenohClientPort(ClientPort):
    def __init__(
        self,
        session: zenoh.Session,
        net: ResolvedNetwork,
        topic: str,
        request_type: type[Message],
        response_type: type[Message],
    ) -> None:
        self._topic = topic
        self._key = f"{net.key_prefix()}/{topic}/@srv"
        self._session = session
        self._response_type = response_type
        # Only used for its matching status (is a server reachable?).
        self._querier = session.declare_querier(self._key)
        self._closed = False

    def call(self, request: Message, timeout: float | None) -> Message:
        effective = _FOREVER_SECONDS if timeout is None else max(timeout, 1e-3)
        replies = self._session.get(
            self._key, payload=request.to_bytes(), timeout=effective
        )
        for reply in replies:
            ok = reply.ok
            if ok is not None:
                return self._response_type.from_bytes(ok.payload.to_bytes())
            err = reply.err
            if err is not None:
                text = err.payload.to_bytes().decode("utf-8", errors="replace")
                if text == "Timeout":  # generated by Zenoh, not by the server
                    raise TimeoutError(
                        f"no response from service '{self._topic}' within {timeout} s"
                    )
                if text.startswith(f"{SchemaMismatchError.__name__}:"):
                    raise SchemaMismatchError(
                        f"the server of '{self._topic}' rejected the request: {text}"
                    )
                raise ServiceError(text)
        if not self.has_server:
            raise NoServerError(f"no server for service '{self._topic}' on Network()")
        raise TimeoutError(
            f"no response from service '{self._topic}' within {timeout} s"
        )

    @property
    def has_server(self) -> bool:
        status: Any = (
            self._querier.matching_status
        )  # the stub says bool; runtime: object
        return bool(getattr(status, "matching", status))

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _undeclare(self._querier)


class ZenohServerPort(ServerPort):
    def __init__(
        self,
        session: zenoh.Session,
        net: ResolvedNetwork,
        topic: str,
        request_type: type[Message],
        response_type: type[Message],
    ) -> None:
        self._topic = topic
        self._request_type = request_type
        self._queries: queue.Queue[zenoh.Query] = queue.Queue()
        self._queryable = session.declare_queryable(
            f"{net.key_prefix()}/{topic}/@srv", self._queries.put
        )
        self._closed = False

    def receive(self, timeout: float) -> tuple[Message, Any] | None:
        deadline = time.monotonic() + timeout
        while True:
            try:
                query = self._queries.get(timeout=max(0.0, deadline - time.monotonic()))
            except queue.Empty:
                return None
            payload = query.payload
            try:
                request = self._request_type.from_bytes(
                    payload.to_bytes() if payload is not None else b""
                )
            except (SchemaMismatchError, ValueError) as e:
                logger.error(
                    "service '%s' received an invalid request: %s", self._topic, e
                )
                self.reply_error(query, f"{type(e).__name__}: {e}")
                continue
            return request, query

    def reply(self, token: Any, response: Message) -> None:
        try:
            token.reply(token.key_expr, response.to_bytes())
        finally:
            token.drop()

    def reply_error(self, token: Any, error: str) -> None:
        try:
            token.reply_err(error.encode("utf-8"))
        finally:
            token.drop()

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            _undeclare(self._queryable)
            while True:
                try:
                    query = self._queries.get_nowait()
                except queue.Empty:
                    break
                try:
                    self.reply_error(query, "AiroIpcError: the server was closed")
                except Exception:  # pragma: no cover - session already closed
                    pass
