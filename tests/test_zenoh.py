"""Integration tests for the Zenoh (network) backend."""

import ctypes
import time

from airo_ipc.api import (
    create_publisher,
    create_requester,
    create_responder,
    create_state_reader,
    create_state_writer,
    create_subscriber,
)
from airo_ipc.buffer import Buffer
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


def test_net_pubsub():
    pub = create_publisher("test/net/pubsub", Point, Transport.NETWORK)
    sub = create_subscriber("test/net/pubsub", Point, Transport.NETWORK)

    # Give subscriber time to register
    time.sleep(0.1)

    pub.publish(Point(x=3.14, y=2.72))
    time.sleep(0.1)

    msg = sub.receive()
    assert msg is not None
    assert abs(msg.x - 3.14) < 1e-10
    assert abs(msg.y - 2.72) < 1e-10


def test_net_reqrep():
    responder = create_responder("test/net/reqrep", Request, Response, Transport.NETWORK)

    def handler(req: Request) -> Response:
        return Response(result=req.value * 5)

    responder.listen(handler)
    time.sleep(0.1)

    requester = create_requester("test/net/reqrep", Request, Response, Transport.NETWORK)
    reply = requester.request(Request(value=4))
    assert reply.result == 20

    responder.stop()


def test_net_state():
    writer = create_state_writer("test/net/state", State, Transport.NETWORK)
    time.sleep(0.1)

    reader = create_state_reader("test/net/state", State, Transport.NETWORK)

    writer.set(State(counter=77))
    time.sleep(0.1)

    state = reader.get()
    assert state is not None
    assert state.counter == 77


def test_net_prefix():
    """Test that net:// prefix works."""
    pub = create_publisher("net://test/prefix/pubsub", Point)
    sub = create_subscriber("net://test/prefix/pubsub", Point)

    time.sleep(0.1)
    pub.publish(Point(x=5.0, y=6.0))
    time.sleep(0.1)

    msg = sub.receive()
    assert msg is not None
    assert msg.x == 5.0
