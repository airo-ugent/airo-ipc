"""A service (client/server) between two processes, on Local() or Network().

Run:
    uv run python examples/service.py
    uv run python examples/service.py --network
"""

from __future__ import annotations

import argparse
import multiprocessing

import airo_ipc as ipc


class AddRequest(ipc.Message):
    a: float
    b: float


class AddResponse(ipc.Message):
    total: float


def add(request: AddRequest) -> AddResponse:
    if request.b < 0:
        raise ValueError("b must be non-negative")
    return AddResponse(total=request.a + request.b)


def serve(transport: ipc.Transport, stop: multiprocessing.synchronize.Event) -> None:  # type: ignore[name-defined]
    with ipc.Context() as ctx:
        ctx.server(
            "example/add", AddRequest, AddResponse, transport, add
        )  # background thread
        stop.wait()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "--network", action="store_true", help="use Network() instead of Local()"
    )
    args = parser.parse_args()
    transport: ipc.Transport = ipc.Network() if args.network else ipc.Local()

    mp = multiprocessing.get_context(
        "spawn"
    )  # share only spawn-context objects with children
    stop = mp.Event()
    server = mp.Process(target=serve, args=(transport, stop))
    server.start()

    client = ipc.client("example/add", AddRequest, AddResponse, transport)
    client.wait_for_server(timeout=10.0)
    print("1 + 2 =", client.call(AddRequest(a=1.0, b=2.0)).total)
    try:
        client.call(AddRequest(a=1.0, b=-1.0))
    except ipc.ServiceError as e:
        print("the server raised:", e.remote_message)
    stop.set()
    server.join()


if __name__ == "__main__":
    main()
