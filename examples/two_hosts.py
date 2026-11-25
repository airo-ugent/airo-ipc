"""Stream between two hosts with Network().

On host A (for example 192.168.1.20):
    uv run python examples/two_hosts.py publish --listen 0.0.0.0
On host B:
    uv run python examples/two_hosts.py subscribe --connect 192.168.1.20

On one LAN with multicast, both sides can also run without --listen / --connect. The endpoints
can come from the environment instead: AIRO_IPC_CONNECT=192.168.1.20.
"""

from __future__ import annotations

import argparse
import time

import numpy as np
from numpy.typing import NDArray

import airo_ipc as ipc


class Pose(ipc.Message):
    position: NDArray[np.float64] = ipc.Array((3,), np.float64)
    orientation: NDArray[np.float64] = ipc.Array(
        (4,), np.float64
    )  # quaternion x, y, z, w


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["publish", "subscribe"])
    parser.add_argument("--connect", default=(), help="host or host:port to connect to")
    parser.add_argument(
        "--listen", default=(), help="address to listen on, e.g. 0.0.0.0"
    )
    args = parser.parse_args()
    transport = ipc.Network(connect=args.connect, listen=args.listen)

    if args.role == "publish":
        pub = ipc.publisher("example/pose", Pose, transport)
        t0 = time.monotonic()
        while True:
            t = time.monotonic() - t0
            pub.publish(
                Pose(position=[np.cos(t), np.sin(t), 0.5], orientation=[0, 0, 0, 1])
            )
            time.sleep(0.01)
    else:
        sub = ipc.subscriber("example/pose", Pose, transport)
        while True:
            pose = sub.receive(timeout=None)
            if pose is not None:
                print(f"seq={pose.meta.seq:6d} position={np.round(pose.position, 3)}")


if __name__ == "__main__":
    main()
