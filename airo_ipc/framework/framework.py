import multiprocessing
from enum import Enum


class IpcKind(Enum):
    DDS = 0,
    SHARED_MEMORY = 1,
