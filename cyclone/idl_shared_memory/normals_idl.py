from dataclasses import dataclass
import numpy as np
from cyclone.idl_shared_memory.base_idl import BaseIDL


@dataclass
class NormalsIDL(BaseIDL):
    timestamp: np.ndarray = np.empty((1,), dtype=np.float64)
    points: np.ndarray = np.empty((1024, 3), dtype=np.float32)
    normals: np.ndarray = np.empty((1024, 3), dtype=np.float32)