"""Transport mode enum for IPC."""

from enum import Enum, auto


class Transport(Enum):
    LOCAL = auto()    # iceoryx2 (shared memory)
    NETWORK = auto()  # Zenoh
