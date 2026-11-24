"""Small-message pub/sub latency (iceoryx2 on Local(), Zenoh on Network()).

One-way send -> receive latency between two processes. The publisher writes
time.monotonic_ns() into the message; the subscriber blocks in receive() (event-based, no
spin-polling) and computes the difference on arrival.
"""

import multiprocessing
import time

from _common import (
    print_summary,
    summarize,
    transport_pairs,
    write_result,
)

import airo_ipc as ipc


class TimedPoint(ipc.Message):
    send_ns: ipc.i64
    x: float
    y: float


def _publisher(topic: str, transport: ipc.Transport, n: int, period_s: float) -> None:
    pub = ipc.publisher(topic, TimedPoint, transport)
    deadline = time.monotonic() + 15
    while not pub.has_subscribers and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    for i in range(n):
        pub.publish(TimedPoint(send_ns=time.monotonic_ns(), x=float(i), y=float(i)))
        time.sleep(period_s)


def run(
    name: str, pair: tuple, n: int = 2000, period_s: float = 0.002, warmup: int = 50
) -> dict:
    topic = f"bench/pubsub_small/{name}"
    pub_transport, sub_transport = pair
    sub = ipc.subscriber(topic, TimedPoint, sub_transport, depth=ipc.MAX_QUEUE_DEPTH)
    proc = multiprocessing.get_context("spawn").Process(
        target=_publisher, args=(topic, pub_transport, n + warmup, period_s)
    )
    proc.start()
    samples_ns: list[int] = []
    received = 0
    deadline = time.monotonic() + (n + warmup) * period_s * 8 + 20
    while received < n + warmup and time.monotonic() < deadline:
        msg = sub.receive(timeout=1.0)
        if msg is None:
            continue
        now = time.monotonic_ns()
        if received >= warmup:
            samples_ns.append(now - msg.send_ns)
        received += 1
    proc.join(timeout=15)
    sub.close()
    return summarize([s / 1000.0 for s in samples_ns], unit="us")


if __name__ == "__main__":
    print("Running 1.0 pub/sub small-message benchmark...")
    payload = {"benchmark": "pubsub_small", "results": {}}
    for name, pair in transport_pairs().items():
        summary = run(name, pair)
        payload["results"][name] = summary
        print_summary(f"pubsub_small / {name}", summary)
    print(f"\nwrote {write_result('pubsub_small', payload)}")
