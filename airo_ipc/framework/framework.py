import multiprocessing
from enum import Enum


class IpcKind(Enum):
    DDS = 0,
    SHARED_MEMORY = 1,


def initialize_ipc():
    """Initialize the IPC framework."""
    multiprocessing.set_start_method("spawn")
