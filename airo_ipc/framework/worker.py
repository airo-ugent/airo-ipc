"""Worker: a long-running process that ticks at a fixed rate and exchanges typed messages.

A Worker bundles three responsibilities deliberately:

1. Process lifecycle. It's a SpawnProcess subclass; the OS-level child is the Worker.
2. A fixed-rate step loop. _setup() runs once, then _step() repeatedly at the configured frequency.
3. IPC endpoints. Publishers and subscribers live in the child process and are torn down with it.

These three are bundled because iceoryx2 Nodes and Zenoh Sessions must be created in the
process that uses them — they don't survive fork() cleanly and hold process-scoped OS resources.
"""

import multiprocessing
import time
from abc import abstractmethod, ABC
from multiprocessing.context import SpawnProcess
from typing import Any, Callable, Dict

from loguru import logger

from airo_ipc.api import create_publisher, create_subscriber
from airo_ipc.buffer import Buffer
from airo_ipc.transport import Transport


class Worker(SpawnProcess, ABC):
    """A long-running process that ticks at a fixed rate and exchanges typed messages with other Workers.

    Subclass and implement `_setup`, `_step`, and `_teardown`. Use `_register_publisher` and
    `_subscribe` from inside `_setup` to wire up IPC.

    This is a convenience class; it is not required to use airo-ipc, but it standardizes the
    "long-running process + IPC" pattern used across the AIRO lab.
    """

    def __init__(self, update_frequency: float, verbose: bool = False):
        super().__init__()

        self._update_frequency = update_frequency
        self._stop_event = multiprocessing.get_context("spawn").Event()
        self._verbose = verbose

    def run(self) -> None:
        self._worker_setup()

        while not self._stop_event.is_set():
            start_time = time.time()

            self._update_subscriptions()

            self._step()

            elapsed_time = time.time() - start_time
            desired_sleep_time = (1.0 / self._update_frequency) - elapsed_time
            sleep_time = max(0.0, desired_sleep_time)

            if self._verbose and desired_sleep_time < 0.0:
                logger.warning(
                    f"Worker {self.__class__.__name__} cannot keep up with desired update frequency {self._update_frequency}Hz!"
                )

            time.sleep(sleep_time)

        self._teardown()

    def _worker_setup(self) -> None:
        """Handles setup that must be done inside the child process."""
        self._subscribers: Dict[str, Any] = dict()
        self._callbacks: Dict[str, Callable] = dict()
        self._publishers: Dict[str, Any] = dict()

        self._setup()

    def _subscribe(
        self,
        topic_name: str,
        buffer_type: type[Buffer],
        transport: Transport = Transport.LOCAL,
        *,
        callback: Callable,
        **kwargs: Any,
    ) -> None:
        if topic_name in self._subscribers:
            if self._verbose:
                logger.warning(
                    f"Worker {self.__class__.__name__} is already subscribed to topic {topic_name}. Ignoring."
                )
            return

        self._subscribers[topic_name] = create_subscriber(
            topic_name, buffer_type, transport, **kwargs
        )
        self._callbacks[topic_name] = callback

    def _update_subscriptions(self) -> None:
        # When a subscription is made in a subscription callback, we should not update it in the same iteration.
        # So we take a snapshot of the items and iterate over that.
        items = list(self._subscribers.items())
        for topic_name, subscriber in items:
            value = subscriber.receive()
            if value is not None:
                self._callbacks[topic_name](value)

    def _register_publisher(
        self,
        topic_name: str,
        buffer_type: type[Buffer],
        transport: Transport = Transport.LOCAL,
        **kwargs: Any,
    ) -> None:
        if topic_name in self._publishers:
            if self._verbose:
                logger.warning(
                    f"Worker {self.__class__.__name__} is already registered as a publisher for topic {topic_name}. Ignoring."
                )
            return

        self._publishers[topic_name] = create_publisher(
            topic_name, buffer_type, transport, **kwargs
        )

    def _publish(self, topic_name: str, value: Buffer) -> None:
        if topic_name not in self._publishers:
            logger.error(
                f"Worker {self.__class__.__name__} does not have a publisher for topic {topic_name}."
            )
            raise RuntimeError(
                f"Cannot publish topics that were not registered. Violating topic: {topic_name}."
            )

        self._publishers[topic_name].publish(value)

    @abstractmethod
    def _setup(self) -> None:
        pass

    @abstractmethod
    def _step(self) -> None:
        pass

    @abstractmethod
    def _teardown(self) -> None:
        pass

    def stop(self) -> None:
        self._stop_event.set()
