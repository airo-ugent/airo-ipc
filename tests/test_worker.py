"""The Worker framework: a spawned process with a fixed-rate loop."""

from __future__ import annotations

import pytest

import airo_ipc as ipc
from airo_ipc.framework import Worker
from conftest import network_pair_transports, unique, wait_until


class Value(ipc.Message):
    x: float


class Doubler(Worker):
    """Doubles every value on '<prefix>/in' to '<prefix>/out', and serves '<prefix>/double'."""

    def __init__(self, prefix: str, transport: ipc.Transport) -> None:
        super().__init__(update_frequency=200.0)
        self._prefix = prefix
        self._transport = transport
        self.steps = 0

    def _setup(self) -> None:
        self._register_publisher(f"{self._prefix}/out", Value, self._transport)
        self._subscribe(
            f"{self._prefix}/in",
            Value,
            self._transport,
            callback=self._on_value,
            qos=ipc.Queue(16),
        )
        self._serve(
            f"{self._prefix}/double",
            Value,
            Value,
            self._transport,
            lambda v: Value(x=2 * v.x),
        )

    def _on_value(self, msg: Value) -> None:
        self._publish(f"{self._prefix}/out", Value(x=2 * msg.x))

    def _step(self) -> None:
        self.steps += 1

    def _teardown(self) -> None:
        pass


@pytest.mark.parametrize("kind", ["local", "network"])
def test_worker_pubsub_and_service(kind: str) -> None:
    prefix = f"test/{unique()}"
    if kind == "local":
        worker_transport: ipc.Transport = ipc.Local()
        test_transport: ipc.Transport = ipc.Local()
    else:
        worker_transport, test_transport = network_pair_transports()

    worker = Doubler(prefix, worker_transport)
    worker.start()
    try:
        with ipc.Context() as ctx:
            out = ctx.subscriber(
                f"{prefix}/out", Value, test_transport, qos=ipc.Queue(16)
            )
            inp = ctx.publisher(f"{prefix}/in", Value, test_transport)
            client = ctx.client(f"{prefix}/double", Value, Value, test_transport)
            assert client.wait_for_server(timeout=20)
            assert wait_until(
                lambda: inp.has_subscribers and out.has_publishers, timeout=20
            )

            for i in range(5):
                inp.publish(Value(x=float(i)))
            received = []
            while len(received) < 5:
                msg = out.receive(timeout=5)
                assert msg is not None
                received.append(msg.x)
            assert received == [0.0, 2.0, 4.0, 6.0, 8.0]
            assert client.call(Value(x=21.0)).x == 42.0
    finally:
        worker.stop()
        worker.join(10)
    assert worker.exitcode == 0


class Idle(Worker):
    def _setup(self) -> None:
        pass

    def _step(self) -> None:
        pass

    def _teardown(self) -> None:
        pass


def test_worker_rejects_invalid_frequency() -> None:
    with pytest.raises(ValueError, match="update_frequency"):
        Idle(update_frequency=0)
