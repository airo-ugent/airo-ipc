"""iceoryx2 backend: local shared memory IPC."""

from __future__ import annotations

import ctypes
import threading
from typing import Callable, Generic, TypeVar

from iceoryx2 import NodeBuilder, ServiceName, ServiceType

from airo_ipc.buffer import Buffer, IncompatibleVersionError

B = TypeVar("B", bound=Buffer)
Req = TypeVar("Req", bound=Buffer)
Rep = TypeVar("Rep", bound=Buffer)

_default_node = None
_node_lock = threading.Lock()


def get_default_node():
    global _default_node
    if _default_node is None:
        with _node_lock:
            if _default_node is None:
                _default_node = NodeBuilder.new().create(ServiceType.Ipc)
    return _default_node


class Iox2Publisher(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, node=None):
        self._buffer_type = buffer_type
        node = node or get_default_node()
        service = (
            node.service_builder(ServiceName.new(topic))
            .publish_subscribe(buffer_type)
            .open_or_create()
        )
        self._publisher = service.publisher_builder().create()

    def publish(self, msg: B) -> None:
        self._publisher.send_copy(msg)


class Iox2Subscriber(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, node=None):
        self._buffer_type = buffer_type
        self._version_checked = False
        node = node or get_default_node()
        service = (
            node.service_builder(ServiceName.new(topic))
            .publish_subscribe(buffer_type)
            .open_or_create()
        )
        self._subscriber = service.subscriber_builder().create()

    def receive(self) -> B | None:
        sample = self._subscriber.receive()
        if sample is None:
            return None
        payload = sample.payload().contents
        if not self._version_checked:
            self._buffer_type.check_version(payload)
            self._version_checked = True
        return payload


class Iox2Requester(Generic[Req, Rep]):
    def __init__(
        self, topic: str, request_type: type[Req], reply_type: type[Rep], *, node=None
    ):
        self._request_type = request_type
        self._reply_type = reply_type
        node = node or get_default_node()
        service = (
            node.service_builder(ServiceName.new(topic))
            .request_response(request_type, reply_type)
            .open_or_create()
        )
        self._client = service.client_builder().create()

    def request(self, msg: Req) -> Rep:
        pending = self._client.send_copy(msg)
        # Busy-poll for the response
        while True:
            response = pending.receive()
            if response is not None:
                return response.payload().contents


class Iox2Responder(Generic[Req, Rep]):
    def __init__(
        self, topic: str, request_type: type[Req], reply_type: type[Rep], *, node=None
    ):
        self._request_type = request_type
        self._reply_type = reply_type
        node = node or get_default_node()
        service = (
            node.service_builder(ServiceName.new(topic))
            .request_response(request_type, reply_type)
            .open_or_create()
        )
        self._server = service.server_builder().create()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def listen(self, handler: Callable[[Req], Rep]) -> None:
        def _serve():
            while not self._stop_event.is_set():
                active_request = self._server.receive()
                if active_request is not None:
                    req = active_request.payload().contents
                    reply = handler(req)
                    active_request.send_copy(reply)

        self._thread = threading.Thread(target=_serve, daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop_event.set()
        if self._thread is not None:
            self._thread.join(timeout=5.0)


class _StateKey(ctypes.Structure):
    """Fixed key for single-value blackboard state."""
    _fields_ = [("id", ctypes.c_uint32)]

    def __eq__(self, other):
        return self.id == other.id


_STATE_KEY = _StateKey(id=0)


class Iox2StateWriter(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, node=None):
        self._buffer_type = buffer_type
        node = node or get_default_node()
        initial = buffer_type()
        service = (
            node.service_builder(ServiceName.new(topic))
            .blackboard_creator(_StateKey)
            .add(_STATE_KEY, initial)
            .create()
        )
        self._writer = service.writer_builder().create()
        self._entry = self._writer.entry(_STATE_KEY, buffer_type)

    def set(self, state: B) -> None:
        self._entry.update_with_copy(state)


class Iox2StateReader(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, node=None):
        self._buffer_type = buffer_type
        node = node or get_default_node()
        service = (
            node.service_builder(ServiceName.new(topic))
            .blackboard_opener(_StateKey)
            .open()
        )
        self._reader = service.reader_builder().create()
        self._entry = self._reader.entry(_STATE_KEY, buffer_type)

    def get(self) -> B | None:
        bb_value = self._entry.get()
        return bb_value.decode_as(self._buffer_type)
