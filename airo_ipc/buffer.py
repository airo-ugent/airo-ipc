"""Buffer base class for IPC data transport using ctypes.Structure."""

import ctypes
from typing import Self

import numpy as np


class IncompatibleVersionError(Exception):
    pass


class _BufferMeta(type(ctypes.Structure)):
    """Metaclass that prepends _ipc_version to _fields_ before ctypes processes them."""

    def __new__(mcs, name, bases, namespace):
        # Only modify subclasses of Buffer, not Buffer itself
        if any(hasattr(b, "_is_buffer_base") for b in bases):
            if "_fields_" in namespace:
                user_fields = namespace["_fields_"]
                namespace["_fields_"] = [
                    ("_ipc_version", ctypes.c_uint32)
                ] + list(user_fields)
        return super().__new__(mcs, name, bases, namespace)


class Buffer(ctypes.Structure, metaclass=_BufferMeta):
    """Base class for IPC buffers.

    Subclasses must define `_version_` (int) and `_fields_` (list of ctypes field tuples).
    The `_ipc_version` field is prepended automatically.

    Example::

        class MyBuffer(Buffer):
            _version_ = 1
            _fields_ = [
                ("x", ctypes.c_double),
                ("y", ctypes.c_double),
                ("data", ctypes.c_uint8 * 1024),
            ]
    """

    _is_buffer_base = True
    _version_: int = 0

    def __init__(self, **kwargs):
        super().__init__(**kwargs)
        self._ipc_version = self.__class__._version_

    def to_bytes(self) -> bytes:
        return bytes(self)

    @classmethod
    def from_bytes(cls, data: bytes) -> Self:
        if len(data) != ctypes.sizeof(cls):
            raise ValueError(
                f"Expected {ctypes.sizeof(cls)} bytes, got {len(data)}"
            )
        return cls.from_buffer_copy(data)

    @classmethod
    def check_version(cls, instance: "Buffer") -> None:
        if instance._ipc_version != cls._version_:
            raise IncompatibleVersionError(
                f"Expected version {cls._version_}, got {instance._ipc_version}"
            )


# Mapping from numpy dtype to ctypes type
_NP_TO_CTYPES = {
    np.dtype(np.uint8): ctypes.c_uint8,
    np.dtype(np.uint16): ctypes.c_uint16,
    np.dtype(np.uint32): ctypes.c_uint32,
    np.dtype(np.uint64): ctypes.c_uint64,
    np.dtype(np.int8): ctypes.c_int8,
    np.dtype(np.int16): ctypes.c_int16,
    np.dtype(np.int32): ctypes.c_int32,
    np.dtype(np.int64): ctypes.c_int64,
    np.dtype(np.float32): ctypes.c_float,
    np.dtype(np.float64): ctypes.c_double,
    np.dtype(np.bool_): ctypes.c_bool,
}


def _make_ctypes_array(dtype: np.dtype, shape: tuple[int, ...]):
    """Build a nested ctypes Array type matching a numpy shape+dtype."""
    base = _NP_TO_CTYPES.get(dtype)
    if base is None:
        raise TypeError(f"Unsupported numpy dtype: {dtype}")
    ct = base
    for dim in reversed(shape):
        ct = ct * dim
    return ct


def buffer_from_arrays(name: str, version: int = 1, **arrays: np.ndarray) -> type[Buffer]:
    """Auto-generate a Buffer subclass from numpy arrays.

    Each keyword argument becomes a field. The resulting class has properties
    that return numpy views over the ctypes array fields.

    Example::

        FrameBuffer = buffer_from_arrays(
            "FrameBuffer",
            bgr=np.zeros((480, 640, 3), dtype=np.uint8),
        )
        buf = FrameBuffer()
        buf.bgr[:] = frame  # numpy assignment
    """
    if not arrays:
        raise ValueError("At least one array must be provided")

    fields = []
    array_info = {}
    for field_name, arr in arrays.items():
        ct_array = _make_ctypes_array(arr.dtype, arr.shape)
        fields.append((f"_{field_name}_data", ct_array))
        array_info[field_name] = (arr.shape, arr.dtype)

    cls = _BufferMeta(name, (Buffer,), {
        "_version_": version,
        "_fields_": fields,
    })

    for field_name, (shape, dtype) in array_info.items():
        data_attr = f"_{field_name}_data"

        def make_property(data_attr=data_attr, shape=shape, dtype=dtype):
            def getter(self):
                raw = getattr(self, data_attr)
                return np.frombuffer(raw, dtype=dtype).reshape(shape)
            return property(getter)

        setattr(cls, field_name, make_property())

    return cls
