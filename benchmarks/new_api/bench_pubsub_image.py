"""Big-buffer pub/sub latency for the new (iceoryx2 + Zenoh) API.

Measures end-to-end latency for a 480x640x3 uint8 image (~900 KB), the size class of a webcam
frame. iceoryx2 should be near-flat; Zenoh pays for serialization + network stack.
"""

import ctypes
import multiprocessing
import sys
import time
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import print_summary, summarize, write_result  # noqa: E402

from airo_ipc.api import create_publisher, create_subscriber  # noqa: E402
from airo_ipc.buffer import Buffer, buffer_from_arrays  # noqa: E402
from airo_ipc.transport import Transport  # noqa: E402

H, W = 480, 640


class _FrameHeader(Buffer):
    """Plain header used to seed the size of the auto-generated frame buffer."""
    _version_ = 1
    _fields_ = [("seq", ctypes.c_uint64), ("send_ts_ns", ctypes.c_int64)]


def _make_buffer_type():
    """Build a buffer with a fixed-size BGR image plus seq+timestamp scalars.

    buffer_from_arrays doesn't currently let us mix arrays and scalars, so we encode the
    timestamp+seq as a tiny ctypes array of int64 at the head of the buffer.
    """
    return buffer_from_arrays(
        "Frame",
        version=1,
        meta=np.zeros((2,), dtype=np.int64),  # [seq, send_ts_ns]
        bgr=np.zeros((H, W, 3), dtype=np.uint8),
    )


def _publisher(topic: str, transport: Transport, n: int, period_s: float):
    Frame = _make_buffer_type()
    pub = create_publisher(topic, Frame, transport)
    time.sleep(0.5)
    template = np.zeros((H, W, 3), dtype=np.uint8)
    for i in range(n):
        m = Frame()
        m.bgr[:] = template
        m.meta[0] = i
        m.meta[1] = time.monotonic_ns()
        pub.publish(m)
        time.sleep(period_s)


def run(transport: Transport, n: int = 200, period_s: float = 0.005, warmup: int = 20) -> dict:
    topic_name = f"bench/new/pubsub_image/{transport.name.lower()}"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_publisher, args=(topic_name, transport, n + warmup, period_s))
    proc.start()

    Frame = _make_buffer_type()
    sub = create_subscriber(topic_name, Frame, transport)

    samples_ns: list[int] = []
    received = 0
    deadline = time.monotonic() + (n + warmup) * period_s * 8 + 15
    while received < n + warmup and time.monotonic() < deadline:
        msg = sub.receive()
        if msg is None:
            continue  # spin-poll
        recv_ts = time.monotonic_ns()
        if received >= warmup:
            send_ts = int(msg.meta[1])
            samples_ns.append(recv_ts - send_ts)
        received += 1

    proc.join(timeout=15)
    samples_us = [s / 1000.0 for s in samples_ns]
    return summarize(samples_us, unit="us")


if __name__ == "__main__":
    print(f"Running new-API pub/sub image benchmark ({H}x{W}x3 = {H*W*3} bytes)...")
    payload = {
        "benchmark": "pubsub_image",
        "api": "new",
        "buffer": f"FrameBuffer ({H*W*3} bytes BGR + 16-byte header)",
        "results": {},
    }
    for transport in (Transport.LOCAL, Transport.NETWORK):
        summary = run(transport)
        payload["results"][transport.name.lower()] = summary
        print_summary(f"new / pubsub_image / {transport.name}", summary)
    out = write_result("new_pubsub_image", payload)
    print(f"\nwrote {out}")
