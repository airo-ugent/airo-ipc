"""Small-message pub/sub latency for the new (iceoryx2 + Zenoh) API.

Measures one-way send→receive latency. Publisher embeds a monotonic timestamp; subscriber reads
it on arrival and computes the delta.
"""

import ctypes
import multiprocessing
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import print_summary, summarize, write_result  # noqa: E402

from airo_ipc.api import create_publisher, create_subscriber  # noqa: E402
from airo_ipc.buffer import Buffer  # noqa: E402
from airo_ipc.transport import Transport  # noqa: E402


class TimedPoint(Buffer):
    _version_ = 1
    _fields_ = [
        ("seq", ctypes.c_uint64),
        ("send_ts_ns", ctypes.c_int64),
        ("x", ctypes.c_double),
        ("y", ctypes.c_double),
    ]


def _publisher(topic: str, transport: Transport, n: int, period_s: float):
    pub = create_publisher(topic, TimedPoint, transport)
    time.sleep(0.5)  # let subscriber come up
    for i in range(n):
        msg = TimedPoint(seq=i, send_ts_ns=time.monotonic_ns(), x=float(i), y=float(i))
        pub.publish(msg)
        time.sleep(period_s)


def run(transport: Transport, n: int = 2000, period_s: float = 0.002, warmup: int = 50) -> dict:
    """Spin-poll the subscriber to avoid contaminating measurements with sleep granularity."""
    topic_name = f"bench/new/pubsub_small/{transport.name.lower()}"
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_publisher, args=(topic_name, transport, n + warmup, period_s))
    proc.start()

    sub = create_subscriber(topic_name, TimedPoint, transport)

    samples_ns: list[int] = []
    received = 0
    deadline = time.monotonic() + (n + warmup) * period_s * 5 + 10
    while received < n + warmup and time.monotonic() < deadline:
        msg = sub.receive()
        if msg is None:
            # Spin without sleep; we want to measure transport latency, not our poll cadence.
            continue
        recv_ts = time.monotonic_ns()
        if received >= warmup:
            samples_ns.append(recv_ts - msg.send_ts_ns)
        received += 1

    proc.join(timeout=10)
    samples_us = [s / 1000.0 for s in samples_ns]
    return summarize(samples_us, unit="us")


if __name__ == "__main__":
    print(f"Running new-API pub/sub small-message benchmark...")
    payload = {
        "benchmark": "pubsub_small",
        "api": "new",
        "buffer": "TimedPoint (32 bytes)",
        "results": {},
    }
    for transport in (Transport.LOCAL, Transport.NETWORK):
        summary = run(transport)
        payload["results"][transport.name.lower()] = summary
        print_summary(f"new / pubsub_small / {transport.name}", summary)

    out = write_result("new_pubsub_small", payload)
    print(f"\nwrote {out}")
