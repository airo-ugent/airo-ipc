"""Cross-process integration tests for both backends.

Existing tests run pub+sub in the same Python process. These spawn a child for the publisher
(or responder) so the real cross-process IPC path is exercised.
"""

import ctypes
import multiprocessing
import time

import numpy as np
import pytest

from airo_ipc.api import (
    create_publisher,
    create_requester,
    create_responder,
    create_state_reader,
    create_state_writer,
    create_subscriber,
)
from airo_ipc.buffer import Buffer, IncompatibleVersionError, buffer_from_arrays
from airo_ipc.transport import Transport


class Point(Buffer):
    _version_ = 1
    _fields_ = [("x", ctypes.c_double), ("y", ctypes.c_double)]


class Request(Buffer):
    _version_ = 1
    _fields_ = [("value", ctypes.c_int32)]


class Response(Buffer):
    _version_ = 1
    _fields_ = [("result", ctypes.c_int32)]


class State(Buffer):
    _version_ = 1
    _fields_ = [("counter", ctypes.c_int32)]


def _publish_then_exit(topic: str, transport: Transport, n: int):
    pub = create_publisher(topic, Point, transport)
    # Brief startup grace so the subscriber is ready (Zenoh in particular).
    time.sleep(0.3)
    for i in range(n):
        pub.publish(Point(x=float(i), y=float(i * 2)))
        time.sleep(0.01)


def _respond_then_exit(topic: str, transport: Transport, stop_after: float):
    responder = create_responder(topic, Request, Response, transport)
    responder.listen(lambda req: Response(result=req.value * 3))
    time.sleep(stop_after)
    responder.stop()


def _write_state_loop(topic: str, transport: Transport, stop_after: float):
    writer = create_state_writer(topic, State, transport)
    end = time.monotonic() + stop_after
    i = 0
    while time.monotonic() < end:
        writer.set(State(counter=i))
        i += 1
        time.sleep(0.01)


def _publish_frames(topic: str, n: int):
    """Module-level helper so spawn can pickle it."""
    FB = buffer_from_arrays("FrameMP", version=1, bgr=np.zeros((8, 8, 3), dtype=np.uint8))
    p = create_publisher(topic, FB, Transport.LOCAL)
    time.sleep(0.3)
    for i in range(n):
        m = FB()
        m.bgr[:] = i % 255
        p.publish(m)
        time.sleep(0.01)


# ---------- iceoryx2 (LOCAL) ----------

def test_local_pubsub_multiprocess():
    ctx = multiprocessing.get_context("spawn")
    topic = "mp/local/pubsub"
    proc = ctx.Process(target=_publish_then_exit, args=(topic, Transport.LOCAL, 20))
    proc.start()
    sub = create_subscriber(topic, Point, Transport.LOCAL)

    received = []
    deadline = time.monotonic() + 5.0
    while len(received) < 5 and time.monotonic() < deadline:
        msg = sub.receive()
        if msg is not None:
            received.append((msg.x, msg.y))
        time.sleep(0.01)
    proc.join(timeout=5.0)
    assert proc.exitcode == 0
    assert len(received) >= 5
    # values should be monotonic in x
    xs = [r[0] for r in received]
    assert xs == sorted(xs)


def test_local_reqrep_multiprocess():
    ctx = multiprocessing.get_context("spawn")
    topic = "mp/local/reqrep"
    proc = ctx.Process(target=_respond_then_exit, args=(topic, Transport.LOCAL, 3.0))
    proc.start()
    time.sleep(0.5)  # let responder come up

    requester = create_requester(topic, Request, Response, Transport.LOCAL)
    reply = requester.request(Request(value=7), timeout=3.0)
    assert reply.result == 21

    proc.join(timeout=5.0)


def test_local_state_multiprocess():
    ctx = multiprocessing.get_context("spawn")
    topic = "mp/local/state"
    proc = ctx.Process(target=_write_state_loop, args=(topic, Transport.LOCAL, 2.0))
    proc.start()
    time.sleep(0.5)

    reader = create_state_reader(topic, State, Transport.LOCAL)
    last = None
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        v = reader.get()
        if v is not None:
            last = v.counter
        time.sleep(0.02)

    proc.join(timeout=5.0)
    assert last is not None
    assert last >= 0


def test_local_version_mismatch_raises():
    """If the wire data carries the wrong _ipc_version, the subscriber raises.

    Note: iceoryx2 catches truly mismatched types at service-open time (different class names,
    different fields). The _ipc_version check is a second line of defense for the case where
    the wire layout is identical but the semantics changed — we simulate that by mutating
    _ipc_version on an outgoing message.
    """
    pub = create_publisher("mp/local/version", Point, Transport.LOCAL)
    sub = create_subscriber("mp/local/version", Point, Transport.LOCAL)

    msg = Point(x=1.0, y=2.0)
    msg._ipc_version = 99  # simulate sender using a different schema version
    pub.publish(msg)
    time.sleep(0.1)
    with pytest.raises(IncompatibleVersionError):
        sub.receive()


def test_local_pubsub_numpy_multiprocess():
    """End-to-end with a buffer_from_arrays type across processes."""
    FrameBuffer = buffer_from_arrays(
        "FrameMP", version=1, bgr=np.zeros((8, 8, 3), dtype=np.uint8)
    )

    ctx = multiprocessing.get_context("spawn")
    topic = "mp/local/frames"
    proc = ctx.Process(target=_publish_frames, args=(topic, 15))
    proc.start()
    sub = create_subscriber(topic, FrameBuffer, Transport.LOCAL)

    seen = []
    deadline = time.monotonic() + 5.0
    while len(seen) < 5 and time.monotonic() < deadline:
        msg = sub.receive()
        if msg is not None:
            seen.append(int(msg.bgr[0, 0, 0]))
        time.sleep(0.01)
    proc.join(timeout=5.0)
    assert len(seen) >= 5


# ---------- Zenoh (NETWORK) ----------

def test_net_pubsub_multiprocess():
    ctx = multiprocessing.get_context("spawn")
    topic = "mp/net/pubsub"
    proc = ctx.Process(target=_publish_then_exit, args=(topic, Transport.NETWORK, 20))
    proc.start()

    sub = create_subscriber(topic, Point, Transport.NETWORK)
    received = []
    deadline = time.monotonic() + 8.0
    while len(received) < 5 and time.monotonic() < deadline:
        msg = sub.receive()
        if msg is not None:
            received.append((msg.x, msg.y))
        time.sleep(0.01)
    proc.join(timeout=8.0)
    assert len(received) >= 5


def test_net_reqrep_multiprocess():
    ctx = multiprocessing.get_context("spawn")
    topic = "mp/net/reqrep"
    proc = ctx.Process(target=_respond_then_exit, args=(topic, Transport.NETWORK, 6.0))
    proc.start()
    time.sleep(2.0)  # responder + multicast scouting; Zenoh discovery is slower than iceoryx2

    requester = create_requester(topic, Request, Response, Transport.NETWORK)
    reply = requester.request(Request(value=4), timeout=5.0)
    assert reply.result == 12

    proc.join(timeout=8.0)


def test_net_state_multiprocess():
    ctx = multiprocessing.get_context("spawn")
    topic = "mp/net/state"
    proc = ctx.Process(target=_write_state_loop, args=(topic, Transport.NETWORK, 3.0))
    proc.start()
    time.sleep(0.6)

    reader = create_state_reader(topic, State, Transport.NETWORK)
    last = None
    deadline = time.monotonic() + 3.0
    while time.monotonic() < deadline:
        v = reader.get(timeout=0.5)
        if v is not None:
            last = v.counter
        time.sleep(0.02)

    proc.join(timeout=6.0)
    assert last is not None
