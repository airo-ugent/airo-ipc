"""iceoryx2 backend: local zero-copy shared-memory IPC.

Publishers and subscribers copy by default for safety (the iceoryx2 Sample owns the underlying
memory and would otherwise dangle on return). For the high-throughput zero-copy fast path, use
`Iox2Subscriber.receive_zero_copy()` and document the lifetime contract in your caller.
"""

from __future__ import annotations

import ctypes
import threading
import time
from typing import Any, Callable, Generic, Optional, TypeVar

from iceoryx2 import NodeBuilder, ServiceName, ServiceType

from airo_ipc.buffer import Buffer
from airo_ipc.defaults import BLACKBOARD, PUB_SUB, REQ_REP, merge_kwargs

B = TypeVar("B", bound=Buffer)
Req = TypeVar("Req", bound=Buffer)
Rep = TypeVar("Rep", bound=Buffer)

# Small sleep used in poll loops to yield CPU without adding meaningful latency.
_POLL_SLEEP_SECONDS = 0.0001  # 100 µs

_default_node = None
_node_lock = threading.Lock()


def get_default_node():
    global _default_node
    if _default_node is None:
        with _node_lock:
            if _default_node is None:
                _default_node = NodeBuilder.new().create(ServiceType.Ipc)
    return _default_node


def _copy_payload(buffer_type: type[B], src) -> B:
    """Copy an iceoryx2 sample's payload into a fresh Buffer instance.

    `src` is the result of `sample.payload().contents` — a reference into memory owned by the
    sample. We memmove it into our own ctypes.Structure so the caller gets a stable object.
    """
    out = buffer_type()
    ctypes.memmove(ctypes.byref(out), ctypes.byref(src), ctypes.sizeof(buffer_type))
    return out


def _build_pubsub_service(node, topic: str, buffer_type, overrides: dict):
    kwargs = merge_kwargs(PUB_SUB, overrides)
    builder = (
        node.service_builder(ServiceName.new(topic))
        .publish_subscribe(buffer_type)
        .max_publishers(kwargs["max_publishers"])
        .max_subscribers(kwargs["max_subscribers"])
        .history_size(kwargs["history_size"])
        .subscriber_max_buffer_size(kwargs["subscriber_max_buffer_size"])
        .subscriber_max_borrowed_samples(kwargs["subscriber_max_borrowed_samples"])
        .enable_safe_overflow(kwargs["enable_safe_overflow"])
    )
    return builder.open_or_create()


def _build_reqrep_service(node, topic: str, req_type, rep_type, overrides: dict):
    kwargs = merge_kwargs(REQ_REP, overrides)
    builder = (
        node.service_builder(ServiceName.new(topic))
        .request_response(req_type, rep_type)
        .max_clients(kwargs["max_clients"])
        .max_servers(kwargs["max_servers"])
        .max_active_requests_per_client(kwargs["max_active_requests_per_client"])
    )
    return builder.open_or_create()


class Iox2Publisher(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, node=None, **kwargs: Any):
        self._buffer_type = buffer_type
        node = node or get_default_node()
        service = _build_pubsub_service(node, topic, buffer_type, kwargs)
        self._publisher = service.publisher_builder().create()

    def publish(self, msg: B) -> None:
        self._publisher.send_copy(msg)


class Iox2Subscriber(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, node=None, **kwargs: Any):
        self._buffer_type = buffer_type
        self._version_checked = False
        node = node or get_default_node()
        service = _build_pubsub_service(node, topic, buffer_type, kwargs)
        self._subscriber = service.subscriber_builder().create()

    def receive(self) -> Optional[B]:
        """Return a stable copy of the latest sample, or None if nothing is available."""
        sample = self._subscriber.receive()
        if sample is None:
            return None
        payload = sample.payload().contents
        if not self._version_checked:
            self._buffer_type.check_version(payload)
            self._version_checked = True
        return _copy_payload(self._buffer_type, payload)

    def receive_zero_copy(self):
        """Return the raw iceoryx2 Sample. Lifetime contract: the returned object MUST be
        kept alive while reading the payload. Use only for high-throughput streaming where
        the cost of a copy is meaningful and you control the read lifetime."""
        sample = self._subscriber.receive()
        if sample is None:
            return None
        if not self._version_checked:
            self._buffer_type.check_version(sample.payload().contents)
            self._version_checked = True
        return sample


class Iox2Requester(Generic[Req, Rep]):
    def __init__(
        self,
        topic: str,
        request_type: type[Req],
        reply_type: type[Rep],
        *,
        node=None,
        **kwargs: Any,
    ):
        self._request_type = request_type
        self._reply_type = reply_type
        self._reply_version_checked = False
        node = node or get_default_node()
        service = _build_reqrep_service(node, topic, request_type, reply_type, kwargs)
        self._client = service.client_builder().create()

    def request(self, msg: Req, timeout: Optional[float] = 5.0) -> Rep:
        """Send a request and wait for the reply.

        Args:
            msg: the request payload.
            timeout: maximum seconds to wait for a reply. Pass None to wait forever.

        Raises:
            TimeoutError: if `timeout` elapses before a reply arrives.
        """
        pending = self._client.send_copy(msg)
        deadline = None if timeout is None else time.monotonic() + timeout
        while True:
            response = pending.receive()
            if response is not None:
                payload = response.payload().contents
                if not self._reply_version_checked:
                    self._reply_type.check_version(payload)
                    self._reply_version_checked = True
                return _copy_payload(self._reply_type, payload)
            if deadline is not None and time.monotonic() >= deadline:
                raise TimeoutError(f"Iox2Requester: no reply within {timeout}s")
            time.sleep(_POLL_SLEEP_SECONDS)


class Iox2Responder(Generic[Req, Rep]):
    def __init__(
        self,
        topic: str,
        request_type: type[Req],
        reply_type: type[Rep],
        *,
        node=None,
        **kwargs: Any,
    ):
        self._request_type = request_type
        self._reply_type = reply_type
        self._request_version_checked = False
        node = node or get_default_node()
        service = _build_reqrep_service(node, topic, request_type, reply_type, kwargs)
        self._server = service.server_builder().create()
        self._stop_event = threading.Event()
        self._thread: Optional[threading.Thread] = None

    def listen(self, handler: Callable[[Req], Rep]) -> None:
        def _serve():
            while not self._stop_event.is_set():
                active_request = self._server.receive()
                if active_request is None:
                    time.sleep(_POLL_SLEEP_SECONDS)
                    continue
                req = active_request.payload().contents
                if not self._request_version_checked:
                    self._request_type.check_version(req)
                    self._request_version_checked = True
                req_copy = _copy_payload(self._request_type, req)
                reply = handler(req_copy)
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
        return isinstance(other, _StateKey) and self.id == other.id

    def __hash__(self):
        return hash(self.id)


_STATE_KEY = _StateKey(id=0)


class Iox2StateWriter(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, node=None, **kwargs: Any):
        self._buffer_type = buffer_type
        node = node or get_default_node()
        bb_kwargs = merge_kwargs(BLACKBOARD, kwargs)
        initial = buffer_type()
        service = (
            node.service_builder(ServiceName.new(topic))
            .blackboard_creator(_StateKey)
            .max_readers(bb_kwargs["max_readers"])
            .max_nodes(bb_kwargs["max_nodes"])
            .add(_STATE_KEY, initial)
            .create()
        )
        self._writer = service.writer_builder().create()
        self._entry = self._writer.entry(_STATE_KEY, buffer_type)

    def set(self, state: B) -> None:
        self._entry.update_with_copy(state)


class Iox2StateReader(Generic[B]):
    def __init__(self, topic: str, buffer_type: type[B], *, node=None, **kwargs: Any):
        self._buffer_type = buffer_type
        self._version_checked = False
        node = node or get_default_node()
        # Blackboard sizing is fixed by the writer/creator side.
        if kwargs:
            unknown = ", ".join(sorted(kwargs))
            raise TypeError(
                f"Iox2StateReader does not accept overrides (got: {unknown}). "
                "Sizing is fixed by the StateWriter that creates the service."
            )
        service = (
            node.service_builder(ServiceName.new(topic))
            .blackboard_opener(_StateKey)
            .open()
        )
        self._reader = service.reader_builder().create()
        self._entry = self._reader.entry(_STATE_KEY, buffer_type)

    def get(self) -> Optional[B]:
        bb_value = self._entry.get()
        if bb_value is None:
            return None
        value = bb_value.decode_as(self._buffer_type)
        if not self._version_checked:
            self._buffer_type.check_version(value)
            self._version_checked = True
        return value
