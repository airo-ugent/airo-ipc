"""Services (client/server), identical on Local() and Network()."""

from __future__ import annotations

import threading
import time

import numpy as np
import pytest
from numpy.typing import NDArray

import airo_ipc as ipc
from conftest import Pair, wait_until


class AddRequest(ipc.Message):
    a: float
    b: float
    delay: float = 0.0
    fail: bool = False


class AddResponse(ipc.Message):
    total: float


class Image(ipc.Message):
    pixels: NDArray[np.uint8] = ipc.Array((None, None), np.uint8)


Small = Image.with_shapes(pixels=(32, 32))
Wrong = Image.with_shapes(pixels=(16, 16))


def add(request: AddRequest) -> AddResponse:
    if request.delay:
        time.sleep(request.delay)
    if request.fail:
        raise ValueError("cannot add")
    return AddResponse(total=request.a + request.b)


def serve(pair: Pair, topic: str, **kwargs: object) -> tuple[ipc.Server, ipc.Client]:  # type: ignore[type-arg]
    server = pair.a.server(topic, AddRequest, AddResponse, pair.ta, add, **kwargs)  # type: ignore[arg-type]
    client = pair.b.client(topic, AddRequest, AddResponse, pair.tb)
    assert client.wait_for_server(timeout=5)
    return server, client


def test_call(pair: Pair, topic: str) -> None:
    _, client = serve(pair, topic)
    response = client.call(AddRequest(a=1.0, b=2.5))
    assert isinstance(response, AddResponse) and response.total == 3.5
    assert client.call(AddRequest(a=-1.0, b=1.0)).total == 0.0


def test_handler_exception_becomes_service_error(pair: Pair, topic: str) -> None:
    _, client = serve(pair, topic)
    with pytest.raises(ipc.ServiceError, match="ValueError: cannot add") as info:
        client.call(AddRequest(a=1.0, b=2.0, fail=True))
    assert info.value.remote_message == "ValueError: cannot add"
    assert (
        client.call(AddRequest(a=1.0, b=2.0)).total == 3.0
    )  # the server keeps serving


def test_handler_returning_wrong_type(pair: Pair, topic: str) -> None:
    pair.a.server(topic, AddRequest, AddResponse, pair.ta, lambda r: AddRequest(a=0.0, b=0.0))  # type: ignore[arg-type, return-value]
    client = pair.b.client(topic, AddRequest, AddResponse, pair.tb)
    assert client.wait_for_server(timeout=5)
    with pytest.raises(ipc.ServiceError, match="TypeError"):
        client.call(AddRequest(a=1.0, b=2.0))


def test_no_server(pair: Pair, topic: str) -> None:
    client = pair.b.client(topic, AddRequest, AddResponse, pair.tb)
    assert not client.has_server
    assert not client.wait_for_server(timeout=0.1)
    start = time.monotonic()
    with pytest.raises(ipc.NoServerError):
        client.call(AddRequest(a=1.0, b=2.0), timeout=5.0)
    assert (
        time.monotonic() - start < 1.0
    )  # fails at once, does not wait for the timeout


def test_timeout(pair: Pair, topic: str) -> None:
    _, client = serve(pair, topic)
    start = time.monotonic()
    with pytest.raises(TimeoutError):
        client.call(AddRequest(a=1.0, b=2.0, delay=0.6), timeout=0.2)
    assert time.monotonic() - start < 0.5
    time.sleep(0.6)
    # The late response to the timed-out call must not be returned for the next call.
    assert client.call(AddRequest(a=2.0, b=2.0)).total == 4.0


def test_call_async_and_concurrent_calls(pair: Pair, topic: str) -> None:
    _, client = serve(pair, topic)
    futures = [client.call_async(AddRequest(a=float(i), b=1.0)) for i in range(10)]
    assert [f.result(timeout=10).total for f in futures] == [i + 1.0 for i in range(10)]


def test_several_clients(pair: Pair, topic: str) -> None:
    serve(pair, topic)
    clients = [pair.b.client(topic, AddRequest, AddResponse, pair.tb) for _ in range(3)]
    results: list[float] = []
    lock = threading.Lock()

    def run(client: ipc.Client, value: float) -> None:  # type: ignore[type-arg]
        assert client.wait_for_server(timeout=5)
        total = client.call(AddRequest(a=value, b=value)).total
        with lock:
            results.append(total)

    threads = [
        threading.Thread(target=run, args=(c, float(i))) for i, c in enumerate(clients)
    ]
    for t in threads:
        t.start()
    for t in threads:
        t.join(10)
    assert sorted(results) == [0.0, 2.0, 4.0]


def test_manual_spin(pair: Pair, topic: str) -> None:
    server, client = serve(pair, topic, spin=False)
    assert server.spin_once(0.01) is False  # nothing to do
    future = client.call_async(AddRequest(a=4.0, b=5.0))
    assert wait_until(lambda: server.spin_once(0.05), timeout=5)
    assert future.result(timeout=5).total == 9.0


def test_spin_once_not_allowed_with_background_thread(pair: Pair, topic: str) -> None:
    server, _ = serve(pair, topic)
    with pytest.raises(RuntimeError, match="spin=True"):
        server.spin_once()


def test_array_payloads(pair: Pair, topic: str) -> None:
    def invert(request: Image) -> Image:
        response = type(request)()
        response.pixels = 255 - request.pixels
        return response

    pair.a.server(topic, Small, Small, pair.ta, invert)
    client = pair.b.client(topic, Small, Small, pair.tb)
    assert client.wait_for_server(timeout=5)
    request = Small()
    request.pixels = np.arange(32 * 32, dtype=np.uint32).reshape(32, 32) % 256
    response = client.call(request)
    assert np.array_equal(response.pixels, 255 - request.pixels)


def test_schema_mismatch(pair: Pair, topic: str) -> None:
    pair.a.server(topic, Small, Small, pair.ta, lambda r: r)
    if not pair.is_network:
        with pytest.raises(ipc.SchemaMismatchError):
            pair.b.client(topic, Wrong, Small, pair.tb)
        return
    client = pair.b.client(topic, Wrong, Small, pair.tb)
    assert client.wait_for_server(timeout=5)
    with pytest.raises(ipc.SchemaMismatchError):
        client.call(Wrong())


def test_second_local_server_fails(topic: str) -> None:
    with ipc.Context() as ctx:
        ctx.server(topic, AddRequest, AddResponse, ipc.Local(), add)
        with pytest.raises(ipc.AiroIpcError, match="too many"):
            ctx.server(topic, AddRequest, AddResponse, ipc.Local(), add)


def test_server_close_stops_serving(pair: Pair, topic: str) -> None:
    server, client = serve(pair, topic)
    assert client.call(AddRequest(a=1.0, b=1.0)).total == 2.0
    server.close()
    assert wait_until(lambda: not client.has_server)
    with pytest.raises(ipc.NoServerError):
        client.call(AddRequest(a=1.0, b=1.0), timeout=1.0)
