import multiprocessing
import time
from abc import abstractmethod, ABC
from multiprocessing.context import SpawnProcess
from typing import Dict, Union, Callable, Any

from cyclonedds.domain import DomainParticipant
from cyclonedds.idl import IdlMeta
from loguru import logger

from airo_ipc.cyclone_shm.idl_shared_memory.base_idl import BaseIdl
from airo_ipc.cyclone_shm.patterns.ddsreader import DDSReader
from airo_ipc.cyclone_shm.patterns.ddswriter import DDSWriter
from airo_ipc.cyclone_shm.patterns.sm_reader import SMReader
from airo_ipc.cyclone_shm.patterns.sm_writer import SMWriter
from airo_ipc.framework.framework import IpcKind


class Node(SpawnProcess, ABC):
    """A Node is a process that can communicate with other Nodes using the IPC framework.
    This is a convenience class: it is not required to use airo-ipc, but it can simplify some of your code,
    abstracting away implementation details and providing a simple publish/subscribe system."""

    def __init__(self, update_frequency: float, verbose: bool = False):
        super().__init__()

        self._update_frequency = update_frequency
        self._stop_event = multiprocessing.get_context("spawn").Event()
        self._verbose = verbose

    def run(self):
        self._node_setup()

        while not self._stop_event.is_set():
            start_time = time.time()

            self._update_subscriptions()

            self._step()

            elapsed_time = time.time() - start_time
            desired_sleep_time = (1.0 / self._update_frequency) - elapsed_time
            sleep_time = max(0.0, desired_sleep_time)

            if self._verbose and desired_sleep_time < 0.0:
                logger.warning(
                    f"Node {self.__class__.__name__} cannot keep up with desired update frequency {self._update_frequency}Hz!")

            time.sleep(sleep_time)

        self._teardown()

    def _node_setup(self):
        """Handles setup that must be done inside the child process."""
        self._cyclone_dp = DomainParticipant()

        self._readers: Dict[str, Union[SMReader, DDSReader]] = dict()
        self._callbacks: Dict[str, Callable] = dict()

        self._writers: Dict[str, Union[SMWriter, DDSWriter]] = dict()

        self._setup()

    def _subscribe(self, topic_name: str, idl_dataclass: Union[IdlMeta, BaseIdl], ipc_kind: IpcKind,
                   callback: Callable):
        if topic_name in self._readers:
            if self._verbose:
                logger.warning(f"Node {self.__class__.__name__} is already subscribed to topic {topic_name}. Ignoring.")
            return

        if ipc_kind == IpcKind.DDS:
            self._readers[topic_name] = DDSReader(self._cyclone_dp, topic_name, idl_dataclass)
        elif ipc_kind == IpcKind.SHARED_MEMORY:
            self._readers[topic_name] = SMReader(self._cyclone_dp, topic_name, idl_dataclass)
        else:
            raise NotImplementedError(f"No implementation available for subscribing with IPC kind {ipc_kind}.")

        self._callbacks[topic_name] = callback

    def _update_subscriptions(self):
        # When a subscription is made in a subscription callback, we should not update it in the same iteration.
        # So we take a snapshot of the items and iterate over that.
        items = list(self._readers.items())
        for topic_name, reader in items:
            value = reader()
            if value is not None:
                self._callbacks[topic_name](value)

    def _register_publisher(self, topic_name: str, idl_dataclass: Union[IdlMeta, BaseIdl], ipc_kind: IpcKind):
        if topic_name in self._writers:
            if self._verbose:
                logger.warning(
                    f"Node {self.__class__.__name__} is already registered as a publisher for topic {topic_name}. Ignoring.")
            return

        if ipc_kind == IpcKind.DDS:
            self._writers[topic_name] = DDSWriter(self._cyclone_dp, topic_name, idl_dataclass)
        elif ipc_kind == IpcKind.SHARED_MEMORY:
            self._writers[topic_name] = SMWriter(self._cyclone_dp, topic_name, idl_dataclass)
        else:
            raise NotImplementedError(f"No implementation available for publishing with IPC kind {ipc_kind}.")

    def _publish(self, topic_name: str, value: Any):
        if topic_name not in self._writers:
            logger.error(f"Node {self.__class__.__name__} does not have a writer for topic {topic_name}.")
            raise RuntimeError(f"Cannot publish topics that were not registered. Violating topic: {topic_name}.")

        self._writers[topic_name](value)

    @abstractmethod
    def _setup(self):
        pass

    @abstractmethod
    def _step(self):
        pass

    @abstractmethod
    def _teardown(self):
        pass

    def stop(self):
        self._stop_event.set()
