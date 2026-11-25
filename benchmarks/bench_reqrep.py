"""Service round-trip latency (Zenoh, on Local() and Network())."""

import multiprocessing
import time

from _common import (
    print_summary,
    summarize,
    transport_pairs,
    write_result,
)

import airo_ipc as ipc


class Req(ipc.Message):
    v: ipc.i64


class Rep(ipc.Message):
    v: ipc.i64


def _serve(
    topic: str, transport: ipc.Transport, stop: "multiprocessing.synchronize.Event"
) -> None:
    with ipc.Context() as ctx:
        server = ctx.server(topic, Req, Rep, transport, lambda r: Rep(v=r.v * 2))
        while not stop.is_set():
            server.spin_once(timeout=0.1)


def run(name: str, pair: tuple, n: int = 1000, warmup: int = 50) -> dict:
    topic = f"bench/reqrep/{name}"
    server_transport, client_transport = pair
    mp = multiprocessing.get_context("spawn")
    stop = mp.Event()
    proc = mp.Process(target=_serve, args=(topic, server_transport, stop))
    proc.start()
    client = ipc.client(topic, Req, Rep, client_transport)
    assert client.wait_for_server(timeout=20), "server did not come up"
    samples_ns: list[int] = []
    for i in range(n + warmup):
        t0 = time.monotonic_ns()
        reply = client.call(Req(v=i), timeout=5.0)
        t1 = time.monotonic_ns()
        assert reply.v == i * 2
        if i >= warmup:
            samples_ns.append(t1 - t0)
    stop.set()
    proc.join(timeout=10)
    client.close()
    return summarize([s / 1000.0 for s in samples_ns], unit="us")


if __name__ == "__main__":
    print("Running 1.0 service round-trip benchmark...")
    payload = {"benchmark": "reqrep", "results": {}}
    for name, pair in transport_pairs().items():
        summary = run(name, pair)
        payload["results"][name] = summary
        print_summary(f"reqrep / {name}", summary)
    print(f"\nwrote {write_result('reqrep', payload)}")
