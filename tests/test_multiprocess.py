"""Cross-process tests: the publisher runs in a spawned child process."""

from __future__ import annotations

import multiprocessing
import os
import signal
import time

import numpy as np
import pytest
from numpy.typing import NDArray

import airo_ipc as ipc
from conftest import network_pair_transports, unique, wait_until


class Image(ipc.Message):
    index: ipc.i32
    pixels: NDArray[np.uint8] = ipc.Array((None, None, 3), np.uint8)


def _publish_forever(
    topic: str, transport: ipc.Transport, shape: tuple[int, int, int]
) -> None:
    msg_type = Image.with_shapes(pixels=shape)
    pub = ipc.publisher(topic, msg_type, transport)
    index = 0
    while True:
        with pub.loan() as frame:
            frame.index = index
            frame.pixels[:] = index % 256
        index += 1
        time.sleep(0.005)


def _start(target: object, *args: object) -> multiprocessing.process.BaseProcess:
    process = multiprocessing.get_context("spawn").Process(
        target=target, args=args, daemon=True
    )
    process.start()
    return process


@pytest.mark.parametrize("kind", ["local", "network"])
def test_cross_process_stream_and_restart_after_kill(kind: str) -> None:
    """A publisher killed with SIGKILL can be restarted; the subscriber keeps working."""
    topic = f"test/{unique()}"
    shape = (120, 160, 3)
    if kind == "local":
        pub_transport: ipc.Transport = ipc.Local()
        sub_transport: ipc.Transport = ipc.Local()
    else:
        pub_transport, sub_transport = network_pair_transports()
    msg_type = Image.with_shapes(pixels=shape)

    with ipc.Context() as ctx:
        publisher = _start(_publish_forever, topic, pub_transport, shape)
        try:
            sub = ctx.subscriber(topic, msg_type, sub_transport)
            first = sub.receive(timeout=20)
            assert first is not None
            assert np.all(first.pixels == first.index % 256)  # no torn frames

            os.kill(publisher.pid, signal.SIGKILL)  # type: ignore[arg-type]
            publisher.join(5)
            assert wait_until(lambda: not sub.has_publishers, timeout=20)

            publisher = _start(_publish_forever, topic, pub_transport, shape)
            assert wait_until(lambda: sub.has_publishers, timeout=20)
            seen = 0
            deadline = time.monotonic() + 20
            while seen < 20 and time.monotonic() < deadline:
                msg = sub.receive(timeout=1)
                if msg is not None:
                    assert np.all(msg.pixels == msg.index % 256)
                    seen += 1
            assert seen == 20
        finally:
            publisher.kill()
            publisher.join(5)


def test_no_shared_memory_leak_after_close() -> None:
    """Closing every endpoint removes the iceoryx2 shared-memory segments of a topic."""
    topic = f"test/{unique()}"
    msg_type = Image.with_shapes(pixels=(16, 16, 3))

    def iox2_files() -> set[str]:
        return {f for f in os.listdir("/dev/shm") if f.startswith("iox2")}

    before = iox2_files()
    with ipc.Context() as ctx:
        pub = ctx.publisher(topic, msg_type, ipc.Local())
        sub = ctx.subscriber(topic, msg_type, ipc.Local())
        pub.publish(msg_type(index=1))
        assert sub.receive(timeout=1) is not None
        assert iox2_files() - before
    assert wait_until(lambda: not (iox2_files() - before), timeout=5), (
        iox2_files() - before
    )


EXIT_SCRIPT = """
import airo_ipc as ipc

class Value(ipc.Message):
    x: float

net = ipc.Network(multicast=False, namespace="{namespace}")
sub = ipc.subscriber("exit/topic", Value, net)
srv = ipc.server("exit/service", Value, Value, net, lambda v: v)
pub = ipc.publisher("exit/topic", Value, net)
pub.publish(Value(x=1.0))
print("done", flush=True)
"""


def test_interpreter_exits_without_close() -> None:
    """Zenoh callbacks must not keep the interpreter alive at exit (no explicit close)."""
    import subprocess
    import sys

    script = EXIT_SCRIPT.format(namespace=unique("ns"))
    result = subprocess.run(
        [sys.executable, "-c", script], capture_output=True, text=True, timeout=30
    )
    assert result.returncode == 0, result.stderr
    assert "done" in result.stdout
