"""Publish/subscribe between two processes, on Local() or Network().

Run:
    uv run python examples/pubsub.py            # Local(): iceoryx2 shared memory
    uv run python examples/pubsub.py --network  # Network(): Zenoh (multicast discovery)

The message class and the code are the same for both transports; only `transport` changes.
"""

from __future__ import annotations

import argparse
import multiprocessing
import time

import numpy as np
from numpy.typing import NDArray

import airo_ipc as ipc


class CameraFrame(ipc.Message):
    frame_id: ipc.u64
    rgb: NDArray[np.uint8] = ipc.Array((480, 640, 3), np.uint8)


def publish(transport: ipc.Transport) -> None:
    pub = ipc.publisher("example/camera", CameraFrame, transport)
    while not pub.has_subscribers:
        time.sleep(0.01)
    for i in range(50):
        with pub.loan() as frame:  # written in place; zero-copy on Local()
            frame.frame_id = i
            frame.rgb[:] = i % 256
        time.sleep(1 / 30)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--network", action="store_true", help="use Network() instead of Local()"
    )
    args = parser.parse_args()
    transport: ipc.Transport = ipc.Network() if args.network else ipc.Local()

    sub = ipc.subscriber("example/camera", CameraFrame, transport)
    publisher = multiprocessing.get_context("spawn").Process(
        target=publish, args=(transport,)
    )
    publisher.start()

    received = 0
    while received < 50:
        frame = sub.receive(timeout=5.0)  # newest frame, an owned copy
        if frame is None:
            break
        latency_ms = (time.time_ns() - frame.meta.stamp_ns) / 1e6
        print(
            f"frame {frame.frame_id:2d}  pixel={frame.rgb[0, 0, 0]:3d}  latency={latency_ms:.2f} ms"
        )
        received += 1
        if frame.frame_id == 49:
            break
    publisher.join()


if __name__ == "__main__":
    main()
