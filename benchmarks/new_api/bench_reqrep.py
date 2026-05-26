"""Request/response round-trip latency for the new API."""

import ctypes
import multiprocessing
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from _common import print_summary, summarize, write_result  # noqa: E402

from airo_ipc.api import create_requester, create_responder  # noqa: E402
from airo_ipc.buffer import Buffer  # noqa: E402
from airo_ipc.transport import Transport  # noqa: E402


class Req(Buffer):
    _version_ = 1
    _fields_ = [("v", ctypes.c_int32)]


class Rep(Buffer):
    _version_ = 1
    _fields_ = [("v", ctypes.c_int32)]


def _responder(topic: str, transport: Transport, duration_s: float):
    r = create_responder(topic, Req, Rep, transport)
    r.listen(lambda x: Rep(v=x.v * 2))
    time.sleep(duration_s)
    r.stop()


def run(transport: Transport, n: int = 500, warmup: int = 20) -> dict:
    topic_name = f"bench/new/reqrep/{transport.name.lower()}"
    ctx = multiprocessing.get_context("spawn")
    duration = (n + warmup) * 0.002 + 5
    proc = ctx.Process(target=_responder, args=(topic_name, transport, duration))
    proc.start()
    time.sleep(2.0)  # zenoh discovery is slow

    req = create_requester(topic_name, Req, Rep, transport)

    samples_ns: list[int] = []
    for i in range(n + warmup):
        t0 = time.monotonic_ns()
        reply = req.request(Req(v=i), timeout=5.0)
        t1 = time.monotonic_ns()
        assert reply.v == i * 2
        if i >= warmup:
            samples_ns.append(t1 - t0)

    proc.join(timeout=10)
    return summarize([s / 1000.0 for s in samples_ns], unit="us")


if __name__ == "__main__":
    print("Running new-API req/rep benchmark...")
    payload = {"benchmark": "reqrep", "api": "new", "results": {}}
    for transport in (Transport.LOCAL, Transport.NETWORK):
        summary = run(transport)
        payload["results"][transport.name.lower()] = summary
        print_summary(f"new / reqrep / {transport.name}", summary)
    out = write_result("new_reqrep", payload)
    print(f"\nwrote {out}")
