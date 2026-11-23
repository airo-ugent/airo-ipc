"""Old-API big-buffer latency over Python shared_memory + DDS sync.

Mirrors benchmarks/new_api/bench_pubsub_image.py — same image size, same N.

Run from v0.2.x checkout.
"""

import multiprocessing
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import print_summary, summarize, write_result  # noqa: E402

from cyclonedds.domain import DomainParticipant  # noqa: E402

from airo_ipc.cyclone_shm.idl_shared_memory.base_idl import BaseIdl  # noqa: E402
from airo_ipc.cyclone_shm.patterns.sm_reader import (
    SMReader,
    WaitingForFirstMessageException,
)  # noqa: E402
from airo_ipc.cyclone_shm.patterns.sm_writer import SMWriter  # noqa: E402

H, W = 480, 640


@dataclass
class Frame(BaseIdl):
    # meta[0] = seq, meta[1] = send_ts_ns
    meta: np.ndarray = field(default_factory=lambda: np.zeros((2,), dtype=np.int64))
    bgr: np.ndarray = field(default_factory=lambda: np.zeros((H, W, 3), dtype=np.uint8))


def _publisher(n: int, period_s: float):
    dp = DomainParticipant()
    template = Frame()
    pub = SMWriter(dp, "bench_old_shm_image", template)
    time.sleep(0.5)
    for i in range(n):
        m = Frame()
        m.meta[0] = i
        m.meta[1] = time.monotonic_ns()
        pub(m)
        time.sleep(period_s)


def run(n: int = 200, period_s: float = 0.005, warmup: int = 20) -> dict:
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_publisher, args=(n + warmup, period_s))
    proc.start()

    dp = DomainParticipant()
    # Wait for writer to be up.
    deadline = time.monotonic() + 10
    sub = None
    while sub is None and time.monotonic() < deadline:
        try:
            sub = SMReader(dp, "bench_old_shm_image", Frame())
        except RuntimeError:
            time.sleep(0.1)
    assert sub is not None, "SMReader could not connect to writer"

    samples_ns: list[int] = []
    received = 0
    last_seq = -1
    deadline = time.monotonic() + (n + warmup) * period_s * 8 + 15
    while received < n + warmup and time.monotonic() < deadline:
        try:
            frame = sub()
        except WaitingForFirstMessageException:
            continue
        if frame is None:
            continue
        seq = int(frame.meta[0])
        if seq == last_seq:
            continue
        last_seq = seq
        recv_ts = time.monotonic_ns()
        if received >= warmup:
            send_ts = int(frame.meta[1])
            samples_ns.append(recv_ts - send_ts)
        received += 1

    proc.join(timeout=15)
    return summarize([s / 1000.0 for s in samples_ns], unit="us")


if __name__ == "__main__":
    print(f"Running old-API SHM image benchmark ({H}x{W}x3 = {H*W*3} bytes)...")
    payload = {
        "benchmark": "pubsub_image",
        "api": "old",
        "transport": "SHM",
        "buffer": f"FrameIdl ({H*W*3} bytes + 16-byte meta)",
        "results": {},
    }
    summary = run()
    payload["results"]["local"] = summary
    print_summary("old / pubsub_image / SHM", summary)
    out = write_result("old_pubsub_image", payload)
    print(f"\nwrote {out}")
