"""Worker: a process that runs a fixed-rate loop and exchanges messages.

A Worker bundles three things on purpose:

1. Process lifecycle. It is a ``multiprocessing`` spawn process; the child process is the Worker.
2. A fixed-rate loop. ``_setup()`` runs once, then ``_step()`` repeatedly, then ``_teardown()``.
3. IPC endpoints. They are created in the child process (iceoryx2 nodes and Zenoh sessions do not
   survive ``fork``) and closed with it.

Subscriber callbacks and service handlers run on the loop thread, before each ``_step()``, so a
Worker needs no locks.

Any ``multiprocessing`` object that you share with a Worker (``Event``, ``Queue``, ...) must come
from the spawn context: ``multiprocessing.get_context("spawn").Event()``.
"""

from __future__ import annotations

import logging
import multiprocessing
import time
from abc import ABC, abstractmethod
from collections.abc import Callable
from multiprocessing.context import SpawnProcess
from typing import Any, TypeVar

from airo_ipc.context import Context
from airo_ipc.endpoints import Client, Publisher, Server, Subscriber
from airo_ipc.message import Message
from airo_ipc.qos import Latest, QoS
from airo_ipc.transport import Transport

__all__ = ["Worker"]

logger = logging.getLogger("airo_ipc")

M = TypeVar("M", bound=Message)
Req = TypeVar("Req", bound=Message)
Rep = TypeVar("Rep", bound=Message)


class Worker(SpawnProcess, ABC):
    """A process that runs ``_step()`` at a fixed rate and exchanges messages.

    Subclass it, implement ``_setup``, ``_step`` and ``_teardown``, and create endpoints in
    ``_setup`` with ``_register_publisher``, ``_subscribe``, ``_serve`` and ``_client``::

        class Doubler(Worker):
            def _setup(self) -> None:
                self._register_publisher("out", Value, ipc.Local())
                self._subscribe("in", Value, ipc.Local(), callback=self._on_value)

            def _on_value(self, msg: Value) -> None:
                self._publish("out", Value(x=2 * msg.x))

            def _step(self) -> None:
                pass

            def _teardown(self) -> None:
                pass

        worker = Doubler(update_frequency=100.0)
        worker.start()
        ...
        worker.stop()
        worker.join()

    Args:
        update_frequency: ``_step()`` calls per second.
        verbose: Log a warning when a loop iteration overruns its period.
    """

    def __init__(self, update_frequency: float, verbose: bool = False) -> None:
        super().__init__()
        if update_frequency <= 0:
            raise ValueError(f"update_frequency must be > 0, got {update_frequency}")
        self._update_frequency = update_frequency
        self._verbose = verbose
        self._stop_event = multiprocessing.get_context("spawn").Event()

    # -- process --------------------------------------------------------------------------

    def run(self) -> None:
        self._context = Context()
        self._publishers: dict[str, Publisher[Any]] = {}
        self._subscribers: dict[str, tuple[Subscriber[Any], Callable[[Any], None]]] = {}
        self._servers: list[Server[Any, Any]] = []
        try:
            self._setup()
            period = 1.0 / self._update_frequency
            next_tick = time.monotonic()
            while not self._stop_event.is_set():
                self._update_subscriptions()
                for server in list(self._servers):
                    while server.spin_once(0.0):
                        pass
                self._step()
                next_tick += period
                delay = next_tick - time.monotonic()
                if delay < 0:
                    if self._verbose:
                        logger.warning(
                            "%s overran its period of %.1f ms by %.1f ms",
                            type(self).__name__,
                            period * 1e3,
                            -delay * 1e3,
                        )
                    next_tick = time.monotonic()  # do not try to catch up
                else:
                    self._stop_event.wait(delay)
        finally:
            try:
                self._teardown()
            finally:
                self._context.close()

    def stop(self) -> None:
        """Ask the worker to finish its current iteration, run ``_teardown()`` and exit."""
        self._stop_event.set()

    # -- endpoints (call from _setup) -----------------------------------------------------

    def _register_publisher(
        self,
        topic: str,
        msg_type: type[M],
        transport: Transport,
        *,
        latched: bool = False,
    ) -> Publisher[M]:
        if topic in self._publishers:
            raise ValueError(
                f"{type(self).__name__} already has a publisher for '{topic}'"
            )
        publisher = self._context.publisher(topic, msg_type, transport, latched=latched)
        self._publishers[topic] = publisher
        return publisher

    def _publish(self, topic: str, msg: Message) -> None:
        publisher = self._publishers.get(topic)
        if publisher is None:
            raise RuntimeError(
                f"{type(self).__name__} has no publisher for '{topic}'; call "
                f"_register_publisher() in _setup()"
            )
        publisher.publish(msg)

    def _publisher(self, topic: str) -> Publisher[Any]:
        """The publisher for ``topic``, for example to use ``loan()``."""
        return self._publishers[topic]

    def _subscribe(
        self,
        topic: str,
        msg_type: type[M],
        transport: Transport,
        *,
        callback: Callable[[M], None],
        qos: QoS = Latest(),
    ) -> Subscriber[M]:
        """Call ``callback(msg)`` on the loop thread for each message, before ``_step()``."""
        if topic in self._subscribers:
            raise ValueError(
                f"{type(self).__name__} is already subscribed to '{topic}'"
            )
        subscriber = self._context.subscriber(topic, msg_type, transport, qos=qos)
        self._subscribers[topic] = (subscriber, callback)
        return subscriber

    def _serve(
        self,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
        handler: Callable[[Req], Rep],
    ) -> Server[Req, Rep]:
        """Serve a service; ``handler`` runs on the loop thread, before ``_step()``."""
        server = self._context.server(
            topic, request_type, response_type, transport, handler, spin=False
        )
        self._servers.append(server)
        return server

    def _client(
        self,
        topic: str,
        request_type: type[Req],
        response_type: type[Rep],
        transport: Transport,
    ) -> Client[Req, Rep]:
        return self._context.client(topic, request_type, response_type, transport)

    def _update_subscriptions(self) -> None:
        # A callback may subscribe to a new topic; iterate over a snapshot.
        for subscriber, callback in list(self._subscribers.values()):
            while True:
                msg = subscriber.receive()
                if msg is None:
                    break
                callback(msg)

    # -- to implement ---------------------------------------------------------------------

    @abstractmethod
    def _setup(self) -> None:
        """Runs once in the child process. Create endpoints here."""

    @abstractmethod
    def _step(self) -> None:
        """Runs at ``update_frequency`` Hz."""

    @abstractmethod
    def _teardown(self) -> None:
        """Runs once when the worker stops, also after an exception."""
