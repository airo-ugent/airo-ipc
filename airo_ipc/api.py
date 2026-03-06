"""Unified factory API for creating IPC publishers, subscribers, and other patterns."""

from __future__ import annotations

from typing import Callable

from airo_ipc.buffer import Buffer
from airo_ipc.transport import Transport


def _parse_topic(topic: str, transport: Transport | None) -> tuple[str, Transport]:
    """Extract transport from topic prefix or use explicit/default transport."""
    if transport is not None:
        # Strip prefix if present even when transport is explicit
        for prefix in ("local://", "net://"):
            if topic.startswith(prefix):
                topic = topic[len(prefix):]
        return topic, transport

    if topic.startswith("local://"):
        return topic[len("local://"):], Transport.LOCAL
    if topic.startswith("net://"):
        return topic[len("net://"):], Transport.NETWORK

    return topic, Transport.LOCAL


def create_publisher(
    topic: str,
    buffer_type: type[Buffer],
    transport: Transport | None = None,
    *,
    session=None,
):
    topic, t = _parse_topic(topic, transport)
    if t == Transport.LOCAL:
        from airo_ipc.backends.iox2 import Iox2Publisher
        return Iox2Publisher(topic, buffer_type, node=session)
    else:
        from airo_ipc.backends.net import ZenohPublisher
        return ZenohPublisher(topic, buffer_type, session=session)


def create_subscriber(
    topic: str,
    buffer_type: type[Buffer],
    transport: Transport | None = None,
    *,
    session=None,
):
    topic, t = _parse_topic(topic, transport)
    if t == Transport.LOCAL:
        from airo_ipc.backends.iox2 import Iox2Subscriber
        return Iox2Subscriber(topic, buffer_type, node=session)
    else:
        from airo_ipc.backends.net import ZenohSubscriber
        return ZenohSubscriber(topic, buffer_type, session=session)


def create_requester(
    topic: str,
    request_type: type[Buffer],
    reply_type: type[Buffer],
    transport: Transport | None = None,
    *,
    session=None,
):
    topic, t = _parse_topic(topic, transport)
    if t == Transport.LOCAL:
        from airo_ipc.backends.iox2 import Iox2Requester
        return Iox2Requester(topic, request_type, reply_type, node=session)
    else:
        from airo_ipc.backends.net import ZenohRequester
        return ZenohRequester(topic, request_type, reply_type, session=session)


def create_responder(
    topic: str,
    request_type: type[Buffer],
    reply_type: type[Buffer],
    transport: Transport | None = None,
    *,
    session=None,
):
    topic, t = _parse_topic(topic, transport)
    if t == Transport.LOCAL:
        from airo_ipc.backends.iox2 import Iox2Responder
        return Iox2Responder(topic, request_type, reply_type, node=session)
    else:
        from airo_ipc.backends.net import ZenohResponder
        return ZenohResponder(topic, request_type, reply_type, session=session)


def create_state_writer(
    topic: str,
    buffer_type: type[Buffer],
    transport: Transport | None = None,
    *,
    session=None,
):
    topic, t = _parse_topic(topic, transport)
    if t == Transport.LOCAL:
        from airo_ipc.backends.iox2 import Iox2StateWriter
        return Iox2StateWriter(topic, buffer_type, node=session)
    else:
        from airo_ipc.backends.net import ZenohStateWriter
        return ZenohStateWriter(topic, buffer_type, session=session)


def create_state_reader(
    topic: str,
    buffer_type: type[Buffer],
    transport: Transport | None = None,
    *,
    session=None,
):
    topic, t = _parse_topic(topic, transport)
    if t == Transport.LOCAL:
        from airo_ipc.backends.iox2 import Iox2StateReader
        return Iox2StateReader(topic, buffer_type, node=session)
    else:
        from airo_ipc.backends.net import ZenohStateReader
        return ZenohStateReader(topic, buffer_type, session=session)
