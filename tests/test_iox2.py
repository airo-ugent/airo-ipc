"""Integration tests for the iceoryx2 (local) backend."""

import ctypes
import time

import numpy as np

from airo_ipc.api import (
    create_publisher,
    create_requester,
    create_responder,
    create_state_reader,
    create_state_writer,
    create_subscriber,
)
from airo_ipc.buffer import Buffer, buffer_from_arrays
from airo_ipc.transport import Transport


class Point(Buffer):
    _version_ = 1
    _fields_ = [
        ("x", ctypes.c_double),
        ("y", ctypes.c_double),
    ]


class Request(Buffer):
    _version_ = 1
    _fields_ = [("value", ctypes.c_int32)]


class Response(Buffer):
    _version_ = 1
    _fields_ = [("result", ctypes.c_int32)]


class State(Buffer):
    _version_ = 1
    _fields_ = [("counter", ctypes.c_int32)]


def test_local_pubsub():
    pub = create_publisher("test_local_pubsub", Point, Transport.LOCAL)
    sub = create_subscriber("test_local_pubsub", Point, Transport.LOCAL)

    pub.publish(Point(x=1.0, y=2.0))
    msg = sub.receive()
    assert msg is not None
    assert msg.x == 1.0
    assert msg.y == 2.0


def test_local_pubsub_numpy():
    FrameBuffer = buffer_from_arrays(
        "FrameBuffer", bgr=np.zeros((4, 6, 3), dtype=np.uint8)
    )

    pub = create_publisher("test_local_pubsub_np", FrameBuffer, Transport.LOCAL)
    sub = create_subscriber("test_local_pubsub_np", FrameBuffer, Transport.LOCAL)

    msg = FrameBuffer()
    msg.bgr[:] = 128
    pub.publish(msg)

    received = sub.receive()
    assert received is not None
    np.testing.assert_array_equal(received.bgr, 128)


def test_local_reqrep():
    responder = create_responder("test_local_reqrep", Request, Response, Transport.LOCAL)

    def handler(req: Request) -> Response:
        return Response(result=req.value * 3)

    responder.listen(handler)

    requester = create_requester("test_local_reqrep", Request, Response, Transport.LOCAL)
    reply = requester.request(Request(value=7))
    assert reply.result == 21

    responder.stop()


def test_local_state():
    writer = create_state_writer("test_local_state", State, Transport.LOCAL)
    reader = create_state_reader("test_local_state", State, Transport.LOCAL)

    writer.set(State(counter=42))
    state = reader.get()
    assert state is not None
    assert state.counter == 42

    writer.set(State(counter=100))
    state = reader.get()
    assert state.counter == 100


def test_local_prefix():
    """Test that local:// prefix works."""
    pub = create_publisher("local://test_prefix_pubsub", Point)
    sub = create_subscriber("local://test_prefix_pubsub", Point)

    pub.publish(Point(x=9.0, y=8.0))
    msg = sub.receive()
    assert msg is not None
    assert msg.x == 9.0
