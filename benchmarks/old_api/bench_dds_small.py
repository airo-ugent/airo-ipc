"""Old-API small-message latency over CycloneDDS.

Run from a checkout where v0.2.x is installed (the cyclone_shm + cyclonedds-nightly stack).
The simplest way: `git checkout master && uv sync` then `uv run python this_script.py`.

Mirrors benchmarks/new_api/bench_pubsub_small.py — same N, same warmup.
"""

import multiprocessing
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import print_summary, summarize, write_result  # noqa: E402

from cyclonedds.domain import DomainParticipant  # noqa: E402
from cyclonedds.idl import IdlStruct  # noqa: E402
from cyclonedds.idl.types import int64, uint64, float64  # noqa: E402

from airo_ipc.cyclone_shm.patterns.ddsreader import DDSReader  # noqa: E402
from airo_ipc.cyclone_shm.patterns.ddswriter import DDSWriter  # noqa: E402


@dataclass
class TimedPoint(IdlStruct):
    seq: uint64
    send_ts_ns: int64
    x: float64
    y: float64


def _publisher(n: int, period_s: float):
    dp = DomainParticipant()
    pub = DDSWriter(dp, "bench_old_pubsub_small", TimedPoint)
    time.sleep(0.5)
    for i in range(n):
        msg = TimedPoint(seq=i, send_ts_ns=time.monotonic_ns(), x=float(i), y=float(i))
        pub(msg)
        time.sleep(period_s)


def run(n: int = 2000, period_s: float = 0.002, warmup: int = 50) -> dict:
    ctx = multiprocessing.get_context("spawn")
    proc = ctx.Process(target=_publisher, args=(n + warmup, period_s))
    proc.start()

    dp = DomainParticipant()
    sub = DDSReader(dp, "bench_old_pubsub_small", TimedPoint)

    samples_ns: list[int] = []
    received = 0
    deadline = time.monotonic() + (n + warmup) * period_s * 5 + 10
    last_seq = -1
    while received < n + warmup and time.monotonic() < deadline:
        msg = sub.take()  # take so we don't re-process the same sample
        if msg is None:
            continue
        if msg.seq == last_seq:
            continue
        last_seq = msg.seq
        recv_ts = time.monotonic_ns()
        if received >= warmup:
            samples_ns.append(recv_ts - msg.send_ts_ns)
        received += 1

    proc.join(timeout=10)
    return summarize([s / 1000.0 for s in samples_ns], unit="us")


if __name__ == "__main__":
    print("Running old-API DDS small-message benchmark...")
    payload = {
        "benchmark": "pubsub_small",
        "api": "old",
        "transport": "DDS",
        "buffer": "TimedPoint IDL struct",
        "results": {},
    }
    summary = run()
    payload["results"]["network"] = summary  # DDS is the network-capable transport
    print_summary("old / pubsub_small / DDS", summary)
    out = write_result("old_pubsub_small", payload)
    print(f"\nwrote {out}")
