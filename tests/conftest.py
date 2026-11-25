"""Shared fixtures: every transport test runs on Local() and on Network() over loopback TCP."""

from __future__ import annotations

import socket
import threading
import time
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass

import pytest

import airo_ipc as ipc


def free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return int(s.getsockname()[1])


def unique(prefix: str = "t") -> str:
    return f"{prefix}{uuid.uuid4().hex[:10]}"


def wait_until(condition: Callable[[], bool], timeout: float = 5.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.01)
    return condition()


def spin_in_thread(server: ipc.Server) -> threading.Thread:  # type: ignore[type-arg]
    """Call ``server.spin_once()`` in a daemon thread until the server is closed."""

    def loop() -> None:
        while not server.closed:
            try:
                server.spin_once(0.05)
            except ipc.ClosedError:
                return

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    return thread


@dataclass
class Pair:
    """Two contexts (A and B) that can reach each other over the transport under test."""

    kind: str
    a: ipc.Context
    b: ipc.Context
    ta: ipc.Transport
    tb: ipc.Transport

    @property
    def is_network(self) -> bool:
        return self.kind == "network"


def network_pair_transports() -> tuple[ipc.Network, ipc.Network]:
    port = free_port()
    namespace = unique("ns")
    ta = ipc.Network(
        listen=f"tcp/127.0.0.1:{port}", multicast=False, namespace=namespace
    )
    tb = ipc.Network(
        connect=f"tcp/127.0.0.1:{port}", multicast=False, namespace=namespace
    )
    return ta, tb


@pytest.fixture(params=["local", "network"])
def pair(request: pytest.FixtureRequest) -> Iterator[Pair]:
    a, b = ipc.Context(), ipc.Context()
    if request.param == "local":
        ta: ipc.Transport = ipc.Local()
        tb: ipc.Transport = ipc.Local()
    else:
        ta, tb = network_pair_transports()
    try:
        yield Pair(request.param, a, b, ta, tb)
    finally:
        b.close()
        a.close()


@pytest.fixture
def topic() -> str:
    return f"test/{unique()}"
