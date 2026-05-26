"""Zenoh backend: network IPC."""

from __future__ import annotations

import threading
import time
from typing import Any, Callable, Generic, Optional, TypeVar

import zenoh

from airo_ipc.buffer import Buffer

B = TypeVar("B", bound=Buffer)
Req = TypeVar("Req", bound=Buffer)
Rep = TypeVar("Rep", bound=Buffer)

_default_session: Optional[zenoh.Session] = None
_session_lock = threading.Lock()

# Track live queryables so we can undeclare them on shutdown — a live queryable holds
# a callback closure that prevents the session from closing and keeps the interpreter alive.
_live_queryables: list = []
_queryables_lock = threading.Lock()


def _register_queryable(q) -> None:
    with _queryables_lock:
        _live_queryables.append(q)


def _shutdown() -> None:
    """Undeclare queryables and close the default session on interpreter shutdown."""
    with _queryables_lock:
        for q in _live_queryables:
            try:
                q.undeclare()
            except Exception:
                pass
        _live_queryables.clear()
    global _default_session
    with _session_lock:
        if _default_session is not None and not _default_session.is_closed():
            try:
                _default_session.close()
            except Exception:
                pass
        _default_session = None


# Register before non-daemon thread join. zenoh queryable callbacks run on non-daemon
# threads that hold the session open; atexit (which runs *after* non-daemon thread join)
# would deadlock. _register_atexit runs before that join.
threading._register_atexit(_shutdown)


def get_default_session() -> zenoh.Session:
    global _default_session
    if _default_session is None:
        with _session_lock:
            if _default_session is None:
                _default_session = zenoh.open(zenoh.Config())
    return _default_session


def configure_default_session(config: zenoh.Config) -> None:
    """Configure the default zenoh session before any backend object is created.

    Call once at process start if you need a non-default config (custom endpoints, scouting
    options, etc.). After any backend object has been created the default session is locked in.
    """
    global _default_session
    with _session_lock:
        if _default_session is not None:
            raise RuntimeError(
                "Default zenoh session already open; configure before creating any "
                "Zenoh-backed publisher/subscriber/etc."
            )
        _default_session = zenoh.open(config)


def _decode_and_check(buffer_type: type[B], data: bytes, version_checked: list) -> B:
    """from_bytes + first-time version check. `version_checked` is a one-element mutable flag."""
    msg = buffer_type.from_bytes(data)
    if not version_checked[0]:
        buffer_type.check_version(msg)
        version_checked[0] = True
    return msg


class ZenohPublisher(Generic[B]):
    def __init__(
        self,
        topic: str,
        buffer_type: type[B],
        *,
        session: Optional[zenoh.Session] = None,
        **kwargs: Any,
    ):
        if kwargs:
            raise TypeError(f"ZenohPublisher: unexpected kwargs {sorted(kwargs)}")
        self._buffer_type = buffer_type
        session = session or get_default_session()
        self._publisher = session.declare_publisher(topic)

    def publish(self, msg: B) -> None:
        self._publisher.put(msg.to_bytes())


class ZenohSubscriber(Generic[B]):
    def __init__(
        self,
        topic: str,
        buffer_type: type[B],
        *,
        session: Optional[zenoh.Session] = None,
        **kwargs: Any,
    ):
        if kwargs:
            raise TypeError(f"ZenohSubscriber: unexpected kwargs {sorted(kwargs)}")
        self._buffer_type = buffer_type
        self._version_checked = [False]
        session = session or get_default_session()
        self._subscriber = session.declare_subscriber(topic)

    def receive(self) -> Optional[B]:
        sample = self._subscriber.try_recv()
        if sample is None:
            return None
        return _decode_and_check(self._buffer_type, bytes(sample.payload), self._version_checked)


class ZenohRequester(Generic[Req, Rep]):
    def __init__(
        self,
        topic: str,
        request_type: type[Req],
        reply_type: type[Rep],
        *,
        session: Optional[zenoh.Session] = None,
        **kwargs: Any,
    ):
        if kwargs:
            raise TypeError(f"ZenohRequester: unexpected kwargs {sorted(kwargs)}")
        self._request_type = request_type
        self._reply_type = reply_type
        self._reply_version_checked = [False]
        self._topic = topic
        self._session = session or get_default_session()

    def request(self, msg: Req, timeout: Optional[float] = 5.0) -> Rep:
        replies = self._session.get(self._topic, payload=msg.to_bytes(), timeout=timeout)
        for reply in replies:
            if reply.ok is not None:
                return _decode_and_check(
                    self._reply_type, bytes(reply.ok.payload), self._reply_version_checked
                )
        raise RuntimeError(f"No reply received for request on '{self._topic}'")


class ZenohResponder(Generic[Req, Rep]):
    def __init__(
        self,
        topic: str,
        request_type: type[Req],
        reply_type: type[Rep],
        *,
        session: Optional[zenoh.Session] = None,
        **kwargs: Any,
    ):
        if kwargs:
            raise TypeError(f"ZenohResponder: unexpected kwargs {sorted(kwargs)}")
        self._request_type = request_type
        self._reply_type = reply_type
        self._request_version_checked = [False]
        self._topic = topic
        self._session = session or get_default_session()
        self._queryable: Optional[zenoh.Queryable] = None

    def listen(self, handler: Callable[[Req], Rep]) -> None:
        def _on_query(query: zenoh.Query):
            payload = bytes(query.payload) if query.payload else b""
            req = _decode_and_check(
                self._request_type, payload, self._request_version_checked
            )
            rep = handler(req)
            query.reply(self._topic, rep.to_bytes())

        self._queryable = self._session.declare_queryable(self._topic, _on_query)
        _register_queryable(self._queryable)

    def stop(self) -> None:
        if self._queryable is not None:
            try:
                self._queryable.undeclare()
            except Exception:
                pass
            with _queryables_lock:
                try:
                    _live_queryables.remove(self._queryable)
                except ValueError:
                    pass
            self._queryable = None


class ZenohStateWriter(Generic[B]):
    """State writer over Zenoh: serves the last `set()` value to any reader query.

    Note: if this writer process dies, the value is lost. There is no Zenoh storage backend.
    """

    def __init__(
        self,
        topic: str,
        buffer_type: type[B],
        *,
        session: Optional[zenoh.Session] = None,
        **kwargs: Any,
    ):
        if kwargs:
            raise TypeError(f"ZenohStateWriter: unexpected kwargs {sorted(kwargs)}")
        self._buffer_type = buffer_type
        self._topic = topic
        self._session = session or get_default_session()
        self._state: Optional[B] = None
        self._lock = threading.Lock()

        def _on_query(query: zenoh.Query):
            with self._lock:
                if self._state is not None:
                    query.reply(self._topic, self._state.to_bytes())

        self._queryable = self._session.declare_queryable(self._topic, _on_query)
        _register_queryable(self._queryable)

    def set(self, state: B) -> None:
        with self._lock:
            self._state = state

    def stop(self) -> None:
        if self._queryable is not None:
            try:
                self._queryable.undeclare()
            except Exception:
                pass
            with _queryables_lock:
                try:
                    _live_queryables.remove(self._queryable)
                except ValueError:
                    pass
            self._queryable = None


class ZenohStateReader(Generic[B]):
    def __init__(
        self,
        topic: str,
        buffer_type: type[B],
        *,
        session: Optional[zenoh.Session] = None,
        **kwargs: Any,
    ):
        if kwargs:
            raise TypeError(f"ZenohStateReader: unexpected kwargs {sorted(kwargs)}")
        self._buffer_type = buffer_type
        self._version_checked = [False]
        self._topic = topic
        self._session = session or get_default_session()

    def get(self, timeout: Optional[float] = 1.0) -> Optional[B]:
        replies = self._session.get(self._topic, timeout=timeout)
        for reply in replies:
            if reply.ok is not None:
                return _decode_and_check(
                    self._buffer_type, bytes(reply.ok.payload), self._version_checked
                )
        return None
