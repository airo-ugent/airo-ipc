"""airo-ipc: one message class, two transports.

``Local()`` uses iceoryx2 zero-copy shared memory on one host; ``Network(...)`` uses Zenoh between
hosts. Publish/subscribe and services (client/server) work the same on both::

    import numpy as np
    from numpy.typing import NDArray
    import airo_ipc as ipc

    class CameraFrame(ipc.Message):
        stamp: float
        rgb: NDArray[np.uint8] = ipc.Array((480, 640, 3), np.uint8)

    pub = ipc.publisher("camera/front", CameraFrame, ipc.Local())
    sub = ipc.subscriber("camera/front", CameraFrame, ipc.Local())
"""

from airo_ipc.api import client, publisher, server, subscriber
from airo_ipc.context import Context, default_context
from airo_ipc.defaults import Iox2Sizing
from airo_ipc.endpoints import Client, Publisher, Server, Subscriber
from airo_ipc.errors import (
    AiroIpcError,
    ClosedError,
    LoanReleasedError,
    NoServerError,
    ReadOnlyMessageError,
    SchemaMismatchError,
    ServiceError,
    TooManyLoansError,
)
from airo_ipc.message import (
    Array,
    Message,
    MessageMeta,
    f32,
    f64,
    i8,
    i16,
    i32,
    i64,
    u8,
    u16,
    u32,
    u64,
)
from airo_ipc.qos import MAX_QUEUE_DEPTH, Latest, QoS, Queue
from airo_ipc.transport import Local, Network, Transport

__all__ = [
    "AiroIpcError",
    "Array",
    "Client",
    "ClosedError",
    "Context",
    "Iox2Sizing",
    "Latest",
    "LoanReleasedError",
    "Local",
    "MAX_QUEUE_DEPTH",
    "Message",
    "MessageMeta",
    "Network",
    "NoServerError",
    "Publisher",
    "QoS",
    "Queue",
    "ReadOnlyMessageError",
    "SchemaMismatchError",
    "Server",
    "ServiceError",
    "Subscriber",
    "TooManyLoansError",
    "Transport",
    "client",
    "default_context",
    "f32",
    "f64",
    "i8",
    "i16",
    "i32",
    "i64",
    "publisher",
    "server",
    "subscriber",
    "u8",
    "u16",
    "u32",
    "u64",
]
