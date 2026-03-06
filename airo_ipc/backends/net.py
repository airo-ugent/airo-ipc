"""Zenoh backend: network IPC."""

from __future__ import annotations

import threading
from typing import Callable, Generic, TypeVar

import zenoh

from airo_ipc.buffer import Buffer

B = TypeVar("B", bound=Buffer)
Req = TypeVar("Req", bound=Buffer)
Rep = TypeVar("Rep", bound=Buffer)

_default_session = None
_session_lock = threading.Lock()


def get_default_session() -> zenoh.Session:
    global _default_session
    if _default_session is None:
        with _session_lock:
            if _default_session is None:
                _default_session = zenoh.open(zenoh.Config())
    return _default_session


class ZenohPublisher(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, session: zenoh.Session | None = None):
        self._buffer_type = buffer_type
        session = session or get_default_session()
        self._publisher = session.declare_publisher(topic)

    def publish(self, msg: B) -> None:
        self._publisher.put(msg.to_bytes())


class ZenohSubscriber(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, session: zenoh.Session | None = None):
        self._buffer_type = buffer_type
        self._version_checked = False
        session = session or get_default_session()
        self._subscriber = session.declare_subscriber(topic)

    def receive(self) -> B | None:
        sample = self._subscriber.try_recv()
        if sample is None:
            return None
        msg = self._buffer_type.from_bytes(bytes(sample.payload))
        if not self._version_checked:
            self._buffer_type.check_version(msg)
            self._version_checked = True
        return msg


class ZenohRequester(Generic[Req, Rep]):
    def __init__(
        self,
        topic: str,
        request_type: type[Req],
        reply_type: type[Rep],
        *,
        session: zenoh.Session | None = None,
    ):
        self._request_type = request_type
        self._reply_type = reply_type
        self._topic = topic
        self._session = session or get_default_session()

    def request(self, msg: Req) -> Rep:
        replies = self._session.get(self._topic, payload=msg.to_bytes())
        for reply in replies:
            if reply.ok is not None:
                return self._reply_type.from_bytes(bytes(reply.ok.payload))
        raise RuntimeError(f"No reply received for request on '{self._topic}'")


class ZenohResponder(Generic[Req, Rep]):
    def __init__(
        self,
        topic: str,
        request_type: type[Req],
        reply_type: type[Rep],
        *,
        session: zenoh.Session | None = None,
    ):
        self._request_type = request_type
        self._reply_type = reply_type
        self._topic = topic
        self._session = session or get_default_session()
        self._queryable = None

    def listen(self, handler: Callable[[Req], Rep]) -> None:
        def _on_query(query):
            payload = bytes(query.payload) if query.payload else b""
            req = self._request_type.from_bytes(payload)
            rep = handler(req)
            query.reply(self._topic, rep.to_bytes())

        self._queryable = self._session.declare_queryable(self._topic, _on_query)

    def stop(self) -> None:
        if self._queryable is not None:
            self._queryable.undeclare()
            self._queryable = None


class ZenohStateWriter(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, session: zenoh.Session | None = None):
        self._buffer_type = buffer_type
        self._topic = topic
        self._session = session or get_default_session()
        self._state: B | None = None
        self._lock = threading.Lock()

        def _on_query(query):
            with self._lock:
                if self._state is not None:
                    query.reply(self._topic, self._state.to_bytes())

        self._queryable = self._session.declare_queryable(self._topic, _on_query)

    def set(self, state: B) -> None:
        with self._lock:
            self._state = state


class ZenohStateReader(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, session: zenoh.Session | None = None):
        self._buffer_type = buffer_type
        self._topic = topic
        self._session = session or get_default_session()

    def get(self) -> B | None:
        replies = self._session.get(self._topic)
        for reply in replies:
            if reply.ok is not None:
                return self._buffer_type.from_bytes(bytes(reply.ok.payload))
        return None
