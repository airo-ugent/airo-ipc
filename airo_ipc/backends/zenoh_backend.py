"""Zenoh backend for ``Network(...)`` (between hosts) and for services on ``Local()``.

Design notes (verified with eclipse-zenoh 1.10.2):

- Wire format: the raw message bytes (header + fields), see :meth:`Message.to_bytes`.
- Key expressions: ``airo_ipc/<namespace>/<topic>`` for topics, ``.../<topic>/@srv`` for
  services, and liveliness tokens under ``.../<topic>/@pub/<id>`` for ``has_publishers``.
  ``has_subscribers`` and ``has_server`` use Zenoh's matching status.
- Zenoh's ``Handler.recv()`` has no timeout, so subscribers use a Python callback and a
  ``threading.Condition``.
- A Python callback keeps the interpreter from exiting until its session is closed, and
  ``atexit`` runs too late for that. Sessions are therefore closed from a
  ``threading._register_atexit`` hook, which runs before Python joins non-daemon threads.
- Zenoh's own shared-memory transport is disabled unless a config file is given.
- Every put, query and reply is sent with ``express=True``. Zenoh batches small messages for up
  to 1 ms by default (``transport/link/tx/queue/batching/time_limit``). With batching, 1–3 % of
  service calls took 1.1 ms instead of 75 µs; with ``express``, none took over 0.5 ms.
- ``Local()`` services use the localhost-only settings of :meth:`Local.resolve
  <airo_ipc.transport.Local.resolve>`: random ports, multicast discovery on loopback.
"""

from __future__ import annotations

import collections
import ctypes
import json
import logging
import queue
import threading
import time
import uuid
import weakref
from typing import Any

import zenoh

from airo_ipc.backends.base import ClientPort, PublisherPort, ServerPort, SubscriberPort
from airo_ipc.errors import (
    AiroIpcError,
    NoServerError,
    SchemaMismatchError,
    ServiceError,
)
from airo_ipc.message import Message
from airo_ipc.transport import Network, ResolvedNetwork, Transport

__all__ = ["ZenohBackend"]

logger = logging.getLogger("airo_ipc")

zenoh.init_log_from_env_or("error")

#: Used for `timeout=None` (Zenoh needs a finite query timeout).
_FOREVER_SECONDS = 365 * 24 * 3600.0

_backends: weakref.WeakSet[ZenohBackend] = weakref.WeakSet()


def _close_all_backends() -> None:
    for backend in list(_backends):
        try:
            backend.close()
        except Exception:  # pragma: no cover - interpreter shutdown
            pass


# Close sessions before Python waits for non-daemon threads at exit (see module doc). This
# module is imported when the first Network() endpoint or service is created.
threading._register_atexit(_close_all_backends)  # type: ignore[attr-defined]


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
    if net.multicast_interface is not None:
        config.insert_json5(
            "scouting/multicast/interface", json.dumps(net.multicast_interface)
        )
    if net.multicast_address is not None:
        config.insert_json5(
            "scouting/multicast/address", json.dumps(net.multicast_address)
        )
    if not net.config:
        config.insert_json5("transport/shared_memory/enabled", "false")
    return config


class ZenohBackend:
    """Owns the Zenoh sessions of one :class:`~airo_ipc.context.Context`.

    Endpoints whose ``Network(...)`` settings are equal (apart from the namespace) share a
    session.
    """

    def __init__(self) -> None:
        self._sessions: dict[tuple[object, ...], zenoh.Session] = {}
        self._lock = threading.Lock()
        self._closed = False
        _backends.add(self)

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
                    hint = (
                        " The port is probably in use by another process: choose another port."
                        if "TCP listener" in str(e)
                        else ""
                    )
                    raise AiroIpcError(
                        f"cannot open a Zenoh session for {net}: {type(e).__name__}: {e}.{hint}"
                    ) from e
                self._sessions[key] = session
            return session

    # The pub/sub factories take the same arguments as those of the iceoryx2 backend.

    def publisher(
        self, transport: Network, topic: str, msg_type: type[Message]
    ) -> PublisherPort:
        net = transport.resolve()
        return ZenohPublisherPort(self.session(net), net, topic, msg_type)

    def subscriber(
        self, transport: Network, topic: str, msg_type: type[Message], depth: int
    ) -> SubscriberPort:
        net = transport.resolve()
        return ZenohSubscriberPort(self.session(net), net, topic, msg_type, depth)

    def client(
        self, transport: Transport, topic: str, req: type[Message], rep: type[Message]
    ) -> ClientPort:
        net = transport.resolve()
        return ZenohClientPort(self.session(net), net, topic, req, rep)

    def server(
        self, transport: Transport, topic: str, req: type[Message], rep: type[Message]
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


def _matching(entity: Any) -> bool:
    """True if a Zenoh publisher or querier has a matching subscriber or queryable."""
    status: Any = (
        entity.matching_status
    )  # the stub says bool; at runtime it has .matching
    return bool(getattr(status, "matching", status))


class _Liveliness:
    """Tracks the liveliness tokens under one key expression (``.../@pub/*``)."""

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
    ) -> None:
        key = f"{net.key_prefix()}/{topic}"
        self._msg_type = msg_type
        self._publisher = session.declare_publisher(key, express=True)
        self._token = session.liveliness().declare_token(
            f"{key}/@pub/{uuid.uuid4().hex}"
        )
        # Buffers of ended loans. put() copies the bytes, so a buffer is free again after it.
        self._free: list[bytearray] = []

    def loan(self) -> tuple[Message, Any]:
        try:
            buffer = self._free.pop()
        except IndexError:
            buffer = bytearray(ctypes.sizeof(self._msg_type))
        return self._msg_type.from_buffer(buffer), buffer

    def send(self, token: Any) -> None:
        try:
            self._publisher.put(token)
        finally:
            self._free.append(token)

    def discard(self, token: Any) -> None:
        self._free.append(token)

    def send_copy(self, msg: Message) -> None:
        self._publisher.put(msg.to_bytes())

    @property
    def has_subscribers(self) -> bool:
        return _matching(self._publisher)

    def close(self) -> None:
        _undeclare(self._token, self._publisher)


class ZenohSubscriberPort(SubscriberPort):
    def __init__(
        self,
        session: zenoh.Session,
        net: ResolvedNetwork,
        topic: str,
        msg_type: type[Message],
        depth: int,
    ) -> None:
        key = f"{net.key_prefix()}/{topic}"
        self._msg_type = msg_type
        # Raw payloads: take() decodes them, so a message dropped from a full queue is never
        # decoded.
        self._queue: collections.deque[zenoh.ZBytes] = collections.deque(maxlen=depth)
        self._cond = threading.Condition()
        self._subscriber = session.declare_subscriber(key, self._on_sample)
        self._publishers = _Liveliness(session, f"{key}/@pub/*")

    def _on_sample(self, sample: zenoh.Sample) -> None:
        if sample.kind != zenoh.SampleKind.PUT:
            return
        with self._cond:
            self._queue.append(sample.payload)
            self._cond.notify_all()

    def take(self) -> tuple[Message, Any] | None:
        with self._cond:
            if not self._queue:
                return None
            payload = self._queue.popleft()
        # Raises SchemaMismatchError if the sender uses a different message definition.
        return self._msg_type.from_bytes(payload.to_bytes()), None

    def release(self, holder: Any) -> None:
        pass

    def wait(self, timeout: float) -> None:
        with self._cond:
            if not self._queue:
                self._cond.wait(timeout)

    @property
    def has_publishers(self) -> bool:
        return self._publishers.any_alive

    def close(self) -> None:
        self._publishers.close()
        _undeclare(self._subscriber)


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

    def call(self, request: Message, timeout: float | None) -> Message:
        effective = _FOREVER_SECONDS if timeout is None else max(timeout, 1e-3)
        replies = self._session.get(
            self._key,
            payload=request.to_bytes(),
            timeout=effective,
            express=True,
            # Deliver the first reply at once; the default waits until the query is finalized.
            consolidation=zenoh.ConsolidationMode.NONE,
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
            raise NoServerError(f"no server for service '{self._topic}'")
        raise TimeoutError(
            f"no response from service '{self._topic}' within {timeout} s"
        )

    @property
    def has_server(self) -> bool:
        return _matching(self._querier)

    def close(self) -> None:
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
            except SchemaMismatchError as e:
                logger.error(
                    "service '%s' received an invalid request: %s", self._topic, e
                )
                self.reply_error(query, e)
                continue
            return request, query

    def reply(self, token: Any, response: Message) -> None:
        try:
            token.reply(token.key_expr, response.to_bytes(), express=True)
        finally:
            token.drop()

    def reply_error(self, token: Any, error: BaseException) -> None:
        try:
            # ZenohClientPort.call parses this "<Type>: <text>" format.
            token.reply_err(f"{type(error).__name__}: {error}".encode("utf-8"))
        finally:
            token.drop()

    def close(self) -> None:
        _undeclare(self._queryable)
        while True:
            try:
                query = self._queries.get_nowait()
            except queue.Empty:
                break
            try:
                self.reply_error(query, AiroIpcError("the server was closed"))
            except Exception:  # pragma: no cover - session already closed
                pass
