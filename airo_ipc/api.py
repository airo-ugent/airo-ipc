"""Module-level factories that use the default :class:`~airo_ipc.context.Context` of the process.

import airo_ipc as ipc

pub = ipc.publisher("camera/front", CameraFrame, ipc.Local())
sub = ipc.subscriber("camera/front", CameraFrame, ipc.Network(connect="10.0.0.2"))
"""

from __future__ import annotations

from collections.abc import Callable
from typing import TypeVar

from airo_ipc.context import default_context
from airo_ipc.endpoints import Client, Publisher, Server, Subscriber
from airo_ipc.message import Message
from airo_ipc.qos import Latest, QoS
from airo_ipc.transport import Transport

__all__ = ["publisher", "subscriber", "client", "server"]

M = TypeVar("M", bound=Message)
Req = TypeVar("Req", bound=Message)
Rep = TypeVar("Rep", bound=Message)


def publisher(
    topic: str, msg_type: type[M], transport: Transport, *, latched: bool = False
) -> Publisher[M]:
    """Create a publisher. See :meth:`Context.publisher <airo_ipc.context.Context.publisher>`."""
    return default_context().publisher(topic, msg_type, transport, latched=latched)


def subscriber(
    topic: str, msg_type: type[M], transport: Transport, *, qos: QoS = Latest()
) -> Subscriber[M]:
    """Create a subscriber. See :meth:`Context.subscriber <airo_ipc.context.Context.subscriber>`."""
    return default_context().subscriber(topic, msg_type, transport, qos=qos)


def client(
    topic: str, request_type: type[Req], response_type: type[Rep], transport: Transport
) -> Client[Req, Rep]:
    """Create a service client. See :meth:`Context.client <airo_ipc.context.Context.client>`."""
    return default_context().client(topic, request_type, response_type, transport)


def server(
    topic: str,
    request_type: type[Req],
    response_type: type[Rep],
    transport: Transport,
    handler: Callable[[Req], Rep],
    *,
    spin: bool = True,
) -> Server[Req, Rep]:
    """Create a service server. See :meth:`Context.server <airo_ipc.context.Context.server>`."""
    return default_context().server(
        topic, request_type, response_type, transport, handler, spin=spin
    )
