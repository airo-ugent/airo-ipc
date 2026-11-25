"""Image pub/sub latency (iceoryx2 on Local(), Zenoh on Network()).

A 480x640x3 uint8 image (~900 KB, a webcam frame). The publisher writes the image with
pub.loan() (zero-copy on Local()). The subscriber reads it either with receive() (an owned copy)
or with receive_loan() (zero-copy on Local()). Latency is measured until the data is readable;
both modes then touch every pixel (sum) outside the timed region.
"""

import multiprocessing
import time

import numpy as np
from numpy.typing import NDArray

from _common import (
    print_summary,
    summarize,
    transport_pairs,
    write_result,
)

import airo_ipc as ipc

H, W = 480, 640


class Frame(ipc.Message):
    send_ns: ipc.i64
    bgr: NDArray[np.uint8] = ipc.Array((H, W, 3), np.uint8)


def _publisher(topic: str, transport: ipc.Transport, n: int, period_s: float) -> None:
    pub = ipc.publisher(topic, Frame, transport)
    deadline = time.monotonic() + 15
    while not pub.has_subscribers and time.monotonic() < deadline:
        time.sleep(0.01)
    time.sleep(0.2)
    image = np.random.default_rng(0).integers(0, 255, size=(H, W, 3), dtype=np.uint8)
    for _ in range(n):
        with pub.loan() as frame:
            frame.bgr[:] = image
            frame.send_ns = time.monotonic_ns()
        time.sleep(period_s)


def run(
    name: str,
    pair: tuple,
    mode: str,
    n: int = 300,
    period_s: float = 0.005,
    warmup: int = 20,
) -> dict:
    topic = f"bench/pubsub_image/{name}/{mode}"
    pub_transport, sub_transport = pair
    sub = ipc.subscriber(topic, Frame, sub_transport, depth=ipc.MAX_QUEUE_DEPTH)
    proc = multiprocessing.get_context("spawn").Process(
        target=_publisher, args=(topic, pub_transport, n + warmup, period_s)
    )
    proc.start()
    samples_ns: list[int] = []
    received = 0
    deadline = time.monotonic() + (n + warmup) * period_s * 8 + 20
    while received < n + warmup and time.monotonic() < deadline:
        # The clock is read as soon as the data is readable: the copy in receive() counts, the
        # pixel sum (a stand-in for processing) does not.
        if mode == "copy":
            msg = sub.receive(timeout=1.0)
            if msg is None:
                continue
            now = time.monotonic_ns()
            send_ns = msg.send_ns
            int(msg.bgr.sum())
        else:
            # "loan_spin" polls without blocking, like the 0.3 benchmark (uses a full CPU core).
            wait = 0.0 if mode == "loan_spin" else 1.0
            with sub.receive_loan(timeout=wait) as view:
                if view is None:
                    continue
                now = time.monotonic_ns()
                send_ns = view.send_ns
                int(view.bgr.sum())
        if received >= warmup:
            samples_ns.append(now - send_ns)
        received += 1
    proc.join(timeout=15)
    sub.close()
    return summarize([s / 1000.0 for s in samples_ns], unit="us")


if __name__ == "__main__":
    print(f"Running 1.0 pub/sub image benchmark ({H}x{W}x3 = {H * W * 3} bytes)...")
    payload = {"benchmark": "pubsub_image", "results": {}}
    for name, pair in transport_pairs().items():
        for mode in ("copy", "loan", "loan_spin"):
            summary = run(name, pair, mode)
            payload["results"][f"{name}_{mode}"] = summary
            print_summary(f"pubsub_image / {name} / {mode}", summary)
    print(f"\nwrote {write_result('pubsub_image', payload)}")
